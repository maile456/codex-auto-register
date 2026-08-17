"""Main PayPal Billing Agreement approval flow orchestrator.

Implements the complete protocol:
  Phase 0: DataDome verification + initial page load
  Phase 1: Device fingerprint + Tealeaf + hCaptcha
  Phase 2: Create account (email submission → signup page)
  Phase 3: Fill signup form + submit (triggers 2FA SMS)
  Phase 4: OTP verification + final authorize mutation
"""
import re
import time
import json
import urllib.parse
from pathlib import Path
from loguru import logger

from paypal.models import (
    SessionState,
    UserInfo,
    CardInfo,
    BillingAddress,
    generate_card,
    generate_random_email,
    generate_user,
    generate_address,
)
from paypal.session import PayPalSession, sanitize_for_log
from paypal.proxy import build_proxy_config, ProxyConfig
from paypal.fingerprint import (
    build_fn_sync_data,
    build_signup_fn_sync_data,
    send_device_fingerprint,
    send_signup_field_events,
)
from paypal.tealeaf import send_tealeaf_data
from paypal.analytics import (
    send_xo_logger,
    send_analytics_ts,
    send_observability_emit,
    send_weasley_log,
)
from paypal.runtime_country_resolver import augment_dynamic_kyc_from_errors
from paypal.graphql import (
    CHECKOUT_SESSION_DATA_QUERY,
    BUYER_CONTEXT_QUERY,
    GRIFFIN_METADATA_QUERY,
    SUPPORTED_FUNDING_SOURCES_QUERY,
    DEFERRED_FEATURE_QUERY,
    INSTALLMENT_OPTIONS_QUERY,
    ADDRESS_AUTOCOMPLETE_FROM_POSTAL_CODE_QUERY,
    INITIATE_2FA_PHONE_MUTATION,
    CONFIRM_2FA_PHONE_MUTATION,
    SIGNUP_NEW_MEMBER_MUTATION,
    AUTHORIZE_BILLING_MUTATION,
)


COUNTRY_PROFILES = {
    "BR": {
        "locale": "pt_BR", "lang": "pt", "phone_code": "+55",
        "accept_language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    },
    "GB": {
        "locale": "en_GB", "lang": "en", "phone_code": "+44",
        "accept_language": "en-GB,en;q=0.9,en-US;q=0.8",
    },
    "BA": {
        "locale": "en_BA", "lang": "en", "phone_code": "+387",
        "accept_language": "bs-BA,bs;q=0.9,hr;q=0.8,sr;q=0.7,en;q=0.6",
    },
    "BH": {
        "locale": "en_US", "lang": "en", "phone_code": "+973",
        "accept_language": "en-BH,en;q=0.9,ar-BH;q=0.8,ar;q=0.7",
    },
    "US": {
        "locale": "en_US", "lang": "en", "phone_code": "+1",
        "accept_language": "en-US,en;q=0.9",
    },
    "JP": {
        "locale": "ja_JP", "lang": "ja", "phone_code": "+81",
        "accept_language": "ja-JP,ja;q=0.9,en-US;q=0.7,en;q=0.5",
    },
    "TH": {
        "locale": "th_TH", "lang": "th", "phone_code": "+66",
        "accept_language": "th-TH,th;q=0.9,en-US;q=0.7,en;q=0.5",
    },
    "ID": {
        "locale": "id_ID", "lang": "id", "phone_code": "+62",
        "accept_language": "id-ID,id;q=0.9,en-US;q=0.7,en;q=0.5",
    },
    "PH": {
        "locale": "en_PH", "lang": "en", "phone_code": "+63",
        "accept_language": "en-PH,en;q=0.9,en-US;q=0.8",
    },
    "TW": {
        "locale": "zh_TW", "lang": "zh", "phone_code": "+886",
        "accept_language": "zh-TW,zh;q=0.9,en-US;q=0.7,en;q=0.5",
    },
    "MX": {
        "locale": "es_MX", "lang": "es", "phone_code": "+52",
        "accept_language": "es-MX,es;q=0.9,en-US;q=0.7,en;q=0.5",
    },
    "AE": {
        "locale": "en_AE", "lang": "en", "phone_code": "+971",
        "accept_language": "en-AE,en;q=0.9,en-US;q=0.8",
    },
    "AU": {
        "locale": "en_AU", "lang": "en", "phone_code": "+61",
        "accept_language": "en-AU,en;q=0.9,en-US;q=0.8",
    },
    "CA": {
        "locale": "en_CA", "lang": "en", "phone_code": "+1",
        "accept_language": "en-CA,en;q=0.9,fr-CA;q=0.8",
    },
    "NL": {
        "locale": "nl_NL", "lang": "nl", "phone_code": "+31",
        "accept_language": "nl-NL,nl;q=0.9,en-US;q=0.7,en;q=0.5",
    },
}


class PayPalFlow:
    def __init__(
        self,
        ba_token: str,
        user: UserInfo,
        card: CardInfo,
        address: BillingAddress,
        max_card_attempts: int = 5,
        proxy_enabled: bool | None = None,
        proxy_index: int | None = None,
        proxy_config: ProxyConfig | None = None,
    ):
        self.ba_token = ba_token
        self.user = user
        if not self.user.email:
            self.user.email = generate_random_email()
        self.card = card
        self.address = address
        self.country = str(address.country or "BR").upper()
        self.profile = COUNTRY_PROFILES.get(self.country)
        if self.profile is None:
            # Dynamic countries share the same protocol phases, while the
            # selected country controls the runtime form/address/KYC schema.
            # Use an English presentation locale and the catalog calling code;
            # never inherit BR metadata for an unrelated country.
            calling_code = "+"
            try:
                catalog_path = Path(__file__).resolve().parents[1] / "data" / "paypal_supported_countries.json"
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                row = next(
                    (
                        item for item in (catalog.get("countries") or [])
                        if isinstance(item, dict)
                        and str(item.get("code") or "").upper() == self.country
                    ),
                    {},
                )
                calling_code = str(row.get("calling_code") or "+")
            except Exception as exc:
                logger.warning("Dynamic country calling-code lookup failed for {}: {}", self.country, exc)
            self.profile = {
                "locale": "en_US",
                "lang": "en",
                "phone_code": calling_code,
                "accept_language": "en-US,en;q=0.9",
            }
        self.locale = self.profile["locale"]
        self.lang = self.profile["lang"]
        self.phone_code = self.profile["phone_code"]
        self.accept_language = self.profile["accept_language"]
        self.max_card_attempts = max(1, max_card_attempts)
        self.proxy_config: ProxyConfig = proxy_config or build_proxy_config(
            enabled=proxy_enabled,
            index=proxy_index,
        )
        self.state = SessionState(ba_token=ba_token)
        # Tracks whether the current address was selected through PayPal's own
        # postcode resolver.  The signup payload must not claim an address is
        # both autocomplete-selected and manually edited at the same time.
        self._address_normalized_by_paypal = False
        self.session = PayPalSession(
            self.state,
            proxy_url=self.proxy_config.url,
            proxy_label=self.proxy_config.label,
            country=self.country,
            locale=self.locale,
        )

    def close(self):
        self.session.close()

    def run(self) -> dict:
        """Execute the complete flow. Returns result dict with status and return_url."""
        try:
            logger.info(f"=== PayPal Billing Agreement Flow ===")
            logger.info("BA Token: {}", sanitize_for_log({"ba_token": self.ba_token})["ba_token"])
            logger.info("Email: {}", sanitize_for_log({"email": self.user.email})["email"])
            logger.info("Phone: {}", sanitize_for_log({"phone": self.user.phone})["phone"])
            logger.debug("Outbound transport ready")

            self._phase0_initial_load()
            self._phase1_risk_controls()
            self._phase2_create_account()
            self._phase3_signup_and_2fa()
            result = self._phase4_authorize()

            if not isinstance(result, dict):
                logger.error("Final flow result had unexpected type: {}", type(result).__name__)
                result = {
                    "status": "error",
                    "error_code": "UNEXPECTED_RESPONSE_TYPE",
                    "error": f"final flow result type was {type(result).__name__}",
                }

            if result.get("status") == "success":
                logger.success(f"=== Flow completed successfully ===")
            else:
                logger.error(f"=== Flow completed with error status ===")
            return result
        except Exception as e:
            logger.error(f"Flow failed: {e}")
            raise
        finally:
            self.close()

    def _phase0_initial_load(self):
        """Load the agreement approval page, handle DataDome if needed."""
        logger.info("--- Phase 0: Initial page load ---")

        url = "https://www.paypal.com/agreements/approve?" + urllib.parse.urlencode({
            "ba_token": self.ba_token,
        })

        # Direct address-bar navigation. curl_cffi supplies UA, client hints
        # and Accept values matching its native TLS/HTTP2 fingerprint.
        resp = self.session.get(url, headers={
            "Accept-Language": self.accept_language,
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-User": "?1",
        })

        if resp.status_code == 403:
            logger.info("Got 403 - DataDome challenge detected")
            manual_response = self._handle_datadome_challenge(resp, url)
            if manual_response is not None:
                resp = manual_response
            else:
                logger.warning("DataDome challenge requires manual solving; attempting protocol fallback...")
                post_url = f"{url}&YWRzZGRjYXB0Y2hh=1"
                resp = self.session.post(post_url, headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.paypal.com",
                }, data={"adsddtoken": ""})

        if resp.status_code == 302:
            redirect_url = resp.headers.get("Location", "")
            logger.info(f"Redirected to: {redirect_url}")
            # Extract ssrt from redirect URL
            ssrt_match = re.search(r"ssrt=(\d+)", redirect_url)
            if ssrt_match:
                self.state.ssrt = ssrt_match.group(1)
            # Follow the redirect
            if redirect_url.startswith("/"):
                redirect_url = f"https://www.paypal.com{redirect_url}"
            resp = self.session.get(redirect_url)

        # Parse the login/signup page
        html = resp.text
        logger.info(f"Page loaded: {resp.status_code}, {len(html)} bytes")
        # Keep the approve page for a deferred Next-Action fallback. The normal
        # guest onboarding path does not need to crawl dozens of JS chunks.
        self._approve_html = html
        self._approve_url = str(resp.url)
        self._approve_response = resp

        # Extract ctxId
        ctx_match = re.search(r'"ctxId"[^"]*"([^"]+)"', html)
        if ctx_match:
            self.state.ctx_id = ctx_match.group(1)
            logger.info(f"Context ID: {self.state.ctx_id}")

        # Extract ssrt if not yet found
        if not self.state.ssrt:
            ssrt_match = re.search(r"ssrt=(\d+)", str(resp.url))
            if not ssrt_match:
                ssrt_match = re.search(r"ssrt=(\d+)", html)
            if ssrt_match:
                self.state.ssrt = ssrt_match.group(1)
                logger.info(f"SSRT: {self.state.ssrt}")

        onboard_match = re.search(
            r'onboardingLink"\s*:\s*"([^"]*?/agreements/approve\?[^"]+)',
            html or "",
            re.I,
        ) or re.search(
            r'href=["\']([^"\']*?ulOnboardRedirect=true[^"\']*)["\']',
            html or "",
            re.I,
        )
        if onboard_match:
            onboard_url = onboard_match.group(1).replace("\\u0026", "&").replace("&amp;", "&").replace("\\/", "/")
            onboard_url = urllib.parse.urljoin(str(resp.url), onboard_url)
            parsed = urllib.parse.urlparse(onboard_url)
            query_items = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            forced = {
                "ul": "1",
                "modxo_redirect_reason": "guest_user",
                "ulOnboardRedirect": "true",
                "ba_token": self.ba_token,
                "locale.x": self.locale,
                "country.x": self.address.country,
            }
            query_items = [(key, value) for key, value in query_items if key not in forced]
            query_items.extend(forced.items())
            self.state.onboarding_url = urllib.parse.urlunparse(
                parsed._replace(query=urllib.parse.urlencode(query_items))
            )
        else:
            params = []
            if self.state.ssrt:
                params.append(("ssrt", self.state.ssrt))
            params.extend([
                ("ul", "1"),
                ("modxo_redirect_reason", "guest_user"),
                ("ulOnboardRedirect", "true"),
                ("ba_token", self.ba_token),
                ("locale.x", self.locale),
                ("country.x", self.address.country),
            ])
            self.state.onboarding_url = "https://www.paypal.com/agreements/approve?" + urllib.parse.urlencode(params)
        # The refreshed /pay Next.js/RSC document embeds the checkout EC
        # directly as `ecToken`. Prefer it over the legacy onboarding route.
        decoded_bootstrap = urllib.parse.unquote(html or "")
        initial_ec_match = re.search(
            r'(?:\\?"ecToken\\?"\s*:\s*\\?"?)(EC-[A-Za-z0-9_-]+)',
            decoded_bootstrap,
            re.I,
        ) or re.search(r'EC-[A-Za-z0-9_-]+', decoded_bootstrap, re.I)
        if initial_ec_match:
            self.state.ec_token = initial_ec_match.group(1) if initial_ec_match.lastindex else initial_ec_match.group(0)
            self.state.onboarding_url = ""
            logger.info(
                "EC Token (Next.js/RSC bootstrap): {}",
                sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"],
            )
        else:
            logger.info("Guest onboarding route prepared")

    def _handle_datadome_challenge(self, response, agreement_url: str):
        """Optional interactive hook. CLI mode keeps the protocol fallback."""
        return None

    @staticmethod
    def _extract_window_initial_data(html: str) -> dict:
        """Extract checkoutweb/weasley window.__INITIAL_DATA__ JSON."""
        # The page contains many reads of window.__INITIAL_DATA__ before the
        # actual server-side assignment.  Anchor on `= {` so we do not parse a
        # JavaScript function body from an earlier reference.
        marker = re.search(r"window\.__INITIAL_DATA__\s*=", html or "")
        if not marker:
            return {}

        start = html.find("{", marker.end())
        if start < 0:
            return {}

        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(html)):
            ch = html[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue

            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[start:idx + 1])
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse __INITIAL_DATA__: {e}")
                        return {}

        return {}

    @staticmethod
    def _extract_content_identifier(html: str, country: str = "BR", lang: str = "pt") -> str:
        """Extract or build the dynamic signup terms contentIdentifier."""
        for pattern in (
            r'"contentIdentifier"\s*:\s*"([^"]*signupTerms[^"]*)"',
            r'\\"contentIdentifier\\"\s*:\s*\\"([^"\\]*signupTerms[^"\\]*)\\"',
            r'([A-Z]{2}:[a-z]{2}:[0-9a-f]{16,64}:compliance\.signupTerms)',
        ):
            match = re.search(pattern, html or "", re.I)
            if match:
                return match.group(1).replace("\\/", "/")
        return f"{country}:{lang}:compliance.signupTerms"

    def _build_signup_url(self) -> str:
        """Build the canonical checkoutweb/signup URL used as GraphQL Referer."""
        params: list[tuple[str, str]] = []
        if self.state.ssrt:
            params.append(("ssrt", self.state.ssrt))
        params.extend([
            ("ul", "1"),
            ("modxo_redirect_reason", "guest_user"),
            ("locale.x", self.locale),
            ("country.x", self.country),
            ("ba_token", self.ba_token),
            ("token", self.state.ec_token),
            ("rcache", "1"),
            ("cookieBannerVariant", "hidden"),
        ])
        return "https://www.paypal.com/checkoutweb/signup?" + urllib.parse.urlencode(params)

    @staticmethod
    def _extract_onboarding_redirect(rsc_text: str) -> str:
        """Extract onboardingRedirectUrl from Next/RSC server-action response."""
        match = re.search(r'"onboardingRedirectUrl"\s*:\s*"([^"]+)"', rsc_text or "")
        if not match:
            return ""
        return match.group(1).replace("\\/", "/")

    @staticmethod
    def _find_access_token(value) -> str:
        """Find an accessToken recursively in GraphQL data/errorData."""
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "accessToken" and isinstance(item, str) and item:
                    return item
                found = PayPalFlow._find_access_token(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = PayPalFlow._find_access_token(item)
                if found:
                    return found
        return ""

    @staticmethod
    def _has_buyer_not_set(result) -> bool:
        items = result if isinstance(result, list) else [result]
        for item in items:
            if not isinstance(item, dict):
                continue
            for err in item.get("errors") or []:
                if not isinstance(err, dict):
                    continue
                data = err.get("data") or {}
                if isinstance(data, dict) and data.get("contingency") == "BUYER_NOT_SET":
                    return True
                if err.get("message") == "BUYER_NOT_SET":
                    return True
        return False

    def _extract_modxo_action_ids(self, html: str, base_url: str):
        """Discover deployment-specific Next server-action IDs from page chunks."""
        action_aliases = {
            "show_create_account_action_id": (
                "showCreateAccountAction", "showCreateAccount", "createAccountAction",
            ),
            "create_user_action_id": (
                "createUserAction", "createUser", "continueToPaymentAction",
            ),
        }

        def scan(text: str) -> bool:
            changed = False
            for attr, aliases in action_aliases.items():
                if getattr(self.state, attr):
                    continue
                candidates: list[tuple[int, str]] = []
                for alias in aliases:
                    for match in re.finditer(re.escape(alias), text or "", re.I):
                        left = max(0, match.start() - 3500)
                        right = min(len(text), match.end() + 3500)
                        window = text[left:right]
                        for id_match in re.finditer(r'["\']([0-9a-f]{32,64})["\']', window, re.I):
                            absolute = left + id_match.start()
                            candidates.append((abs(absolute - match.start()), id_match.group(1)))
                if candidates:
                    action_id = min(candidates, key=lambda item: item[0])[1]
                    setattr(self.state, attr, action_id)
                    logger.info("ModXO action {} discovered", attr)
                    changed = True
            return changed

        scan(html or "")
        if self.state.show_create_account_action_id and self.state.create_user_action_id:
            return

        script_urls: list[str] = []
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html or "", re.I):
            if "_next/static/" not in src:
                continue
            url = urllib.parse.urljoin(base_url, src.replace("\\/", "/"))
            if url not in script_urls:
                script_urls.append(url)
        logger.info("ModXO action scan: {} page chunks", len(script_urls))

        for script_url in script_urls[:120]:
            try:
                js_resp = self.session.get(
                    script_url,
                    headers={
                        "Accept": "*/*",
                        "Referer": base_url,
                        "Sec-Fetch-Dest": "script",
                        "Sec-Fetch-Mode": "no-cors",
                        "Sec-Fetch-Site": "same-origin",
                    },
                )
                if js_resp.status_code == 200:
                    scan(js_resp.text)
                if self.state.show_create_account_action_id and self.state.create_user_action_id:
                    return
            except Exception as e:
                logger.debug("Failed to inspect ModXO chunk {}: {}", script_url, e)

    def _card_issuer_type(self) -> str:
        """PayPal GraphQL CardIssuerType enum."""
        prefix2 = int(self.card.number[:2]) if self.card.number[:2].isdigit() else 0
        prefix4 = int(self.card.number[:4]) if self.card.number[:4].isdigit() else 0
        if 51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720:
            return "MASTER_CARD"
        if self.card.number.startswith("4"):
            return "VISA"
        if self.card.number.startswith("3"):
            return "AMEX"
        if self.card.number.startswith("6"):
            return "DISCOVER"
        return "VISA"

    def _masked_card_number(self) -> str:
        return sanitize_for_log({"cardNumber": self.card.number})["cardNumber"]

    def _masked_phone(self) -> str:
        return sanitize_for_log({"phone": self.user.phone})["phone"]

    def _update_user_phone(self, phone: str):
        """Update country-specific phone fields used by signup/2FA calls."""
        raw = (phone or "").strip()
        if raw.lower().startswith("phone:"):
            raw = raw.split(":", 1)[1].strip()

        digits = "".join(ch for ch in raw if ch.isdigit())
        if len(digits) < 8:
            raise ValueError("phone number is too short")

        calling_code = self.phone_code.lstrip("+")
        if digits.startswith(calling_code) and len(digits) > len(calling_code) + 7:
            country_code = self.phone_code
            local = digits[len(calling_code):]
            full = f"+{digits}"
        else:
            country_code = self.phone_code
            local = digits[1:] if self.country in {"GB", "JP", "TH", "ID", "PH", "TW", "MX", "AE", "AU"} and digits.startswith("0") else digits
            full = f"{self.phone_code}{local}"

        if len(local) < 8:
            raise ValueError("local phone number is too short")

        self.user.phone = full
        self.user.phone_country_code = country_code
        self.user.phone_local = local
        logger.info("Phone updated for OTP retry: {}", self._masked_phone())

    def _initiate_2fa_phone_confirmation(self, token: str, signup_url: str) -> tuple[str, str]:
        """Send a new 2FA SMS and return authId/challengeId."""
        logger.info("Step 1: Initiating 2FA phone confirmation for {}...", self._masked_phone())
        send_weasley_log(
            self.session,
            self.state.ec_token,
            signup_url,
            [
                "weasley_risk_based_phone_confirmation_modal_component_mounted",
                "weasley_initiate_phone_confirmation_start",
                "weasley_api_request_initiate_risk_based_two_factor_phone_confirmation_mutation",
            ],
            country=self.address.country,
            lang=self.lang,
        )
        initiate_result = self.session.graphql(
            "InitiateRiskBasedTwoFactorPhoneConfirmationMutation",
            INITIATE_2FA_PHONE_MUTATION,
            {
                "phoneNumber": self.user.phone_local,
                "locale": {"country": self.country, "lang": self.lang},
                "phoneCountry": self.country,
                "token": token,
            },
        )
        logger.info(
            "2FA initiation result (sanitized): {}",
            json.dumps(sanitize_for_log(initiate_result), ensure_ascii=False, indent=2)[:500],
        )

        result_obj = initiate_result[0] if isinstance(initiate_result, list) else initiate_result
        tfa_data = result_obj.get("data", {}).get(
            "initiateRiskBasedTwoFactorPhoneConfirmation", {}
        )
        auth_id = tfa_data.get("authId", "")
        challenge_id = tfa_data.get("challengeId", "")
        state = tfa_data.get("state", "")
        logger.info("2FA state: {}, authId=<redacted>, challengeId=<redacted>", state)

        if state != "PENDING":
            if state == "SMS_LIMIT_EXCEEDED":
                raise RuntimeError(
                    "SMS_LIMIT_EXCEEDED: this phone/session has reached the SMS send limit; use a new phone"
                )
            raise RuntimeError(f"2FA initiation was not accepted: state={state or '<missing>'}")
        if not auth_id or not challenge_id:
            raise RuntimeError("Failed to get authId/challengeId from 2FA initiation")
        return auth_id, challenge_id

    def _confirm_2fa_phone_confirmation(
        self,
        token: str,
        signup_url: str,
        auth_id: str,
        challenge_id: str,
        otp: str,
    ) -> bool:
        """Confirm one OTP attempt. Return True only on CONFIRMED."""
        logger.info("Step 2: Confirming OTP: <redacted>")
        send_weasley_log(
            self.session,
            self.state.ec_token,
            signup_url,
            [
                "weasley_confirm_phone_confirmation_start",
                "weasley_api_request_confirm_risk_based_two_factor_phone_confirmation_mutation",
            ],
            country=self.address.country,
            lang=self.lang,
        )
        confirm_result = self.session.graphql(
            "ConfirmRiskBasedTwoFactorPhoneConfirmationMutation",
            CONFIRM_2FA_PHONE_MUTATION,
            {
                "pin": otp,
                "authId": auth_id,
                "challengeId": challenge_id,
                "token": token,
            },
        )
        logger.info(
            "OTP confirmation result (sanitized): {}",
            json.dumps(sanitize_for_log(confirm_result), ensure_ascii=False, indent=2)[:500],
        )

        result_obj = confirm_result[0] if isinstance(confirm_result, list) else confirm_result
        confirm_data = result_obj.get("data", {}).get(
            "confirmRiskBasedTwoFactorPhoneConfirmation", {}
        ) or {}
        confirm_state = confirm_data.get("state", "")
        if confirm_state == "CONFIRMED":
            logger.success("OTP confirmed successfully!")
            return True

        errors = result_obj.get("errors") or []
        if errors:
            logger.warning(
                "OTP confirmation failed with errors: {}",
                json.dumps(sanitize_for_log(errors), ensure_ascii=False, indent=2),
            )
        else:
            logger.warning("OTP confirmation failed, state: {}", confirm_state or "<missing>")
        return False

    def _confirm_phone_with_retry(self, token: str, signup_url: str):
        """Loop until OTP is confirmed; user can enter a new phone to resend."""
        while True:
            try:
                auth_id, challenge_id = self._initiate_2fa_phone_confirmation(token, signup_url)
            except Exception as e:
                logger.error("Failed to initiate OTP for {}: {}", self._masked_phone(), e)
                while True:
                    value = input(
                        "\n>>> 发送验证码失败。请输入新的手机号重新发送"
                        "（如 +5591980133818）；输入 q 退出: "
                    ).strip()
                    if value.lower() in {"q", "quit", "exit"}:
                        raise RuntimeError("OTP confirmation cancelled by user") from e
                    try:
                        self._update_user_phone(value)
                        break
                    except ValueError as phone_error:
                        logger.warning("手机号无效：{}。请重新输入。", phone_error)
                continue
            logger.info("SMS verification code sent to phone: {}", self._masked_phone())

            while True:
                value = input(
                    "\n>>> 输入6位短信验证码；如需换号，直接输入新手机号"
                    "（如 +5591980133818 或 phone:+5591980133818）；输入 q 退出: "
                ).strip()

                if value.lower() in {"q", "quit", "exit"}:
                    raise RuntimeError("OTP confirmation cancelled by user")

                if len(value) == 6 and value.isdigit():
                    if self._confirm_2fa_phone_confirmation(
                        token,
                        signup_url,
                        auth_id,
                        challenge_id,
                        value,
                    ):
                        return
                    logger.warning(
                        "验证码验证失败。可以继续输入新的6位验证码，或输入新手机号重新发送验证码。"
                    )
                    continue

                try:
                    self._update_user_phone(value)
                    logger.info("Re-sending OTP to the new phone...")
                    break
                except ValueError as e:
                    logger.warning(
                        "输入既不是6位验证码，也不是有效手机号：{}。请重新输入。",
                        e,
                    )

    def _card_expiration_date(self) -> str:
        exp_parts = self.card.expiry.split("/")
        return f"{exp_parts[0]}/{exp_parts[1]}" if len(exp_parts) == 2 else self.card.expiry

    def _dob_payload(self) -> dict:
        dob_parts = self.user.dob.split("/")
        return (
            {"day": dob_parts[0], "month": dob_parts[1], "year": dob_parts[2]}
            if len(dob_parts) == 3
            else {}
        )

    def _runtime_kyc_fields(self) -> dict[str, bool]:
        schema = getattr(self, "runtime_form_schema", None) or {}
        fields = ((schema.get("kyc") or {}).get("fields") or [])
        return {
            str(item.get("name")): bool(item.get("required"))
            for item in fields
            if isinstance(item, dict) and item.get("name")
        }

    def _apply_runtime_kyc_variables(self, variables: dict) -> None:
        observed = self._runtime_kyc_fields()
        if not observed:
            return
        nationality = self.user.nationality or self.country
        document_type = self.user.identity_document_type
        document_number = self.user.identity_document_number
        # Page bundles contain controls for many countries.  Merely observing a
        # token in the bundle does not mean that field belongs in this country's
        # mutation.  Only merge fields explicitly marked required by runtime
        # metadata/error feedback; known country-specific fields are handled by
        # the dedicated branches below.
        if observed.get("nationality"):
            variables["nationality"] = nationality
        if observed.get("place_of_birth"):
            variables["placeOfBirth"] = self.user.place_of_birth or nationality
        if observed.get("middle_name") and self.user.middle_name:
            variables["middleName"] = self.user.middle_name
        if observed.get("occupation") and self.user.occupation:
            variables["occupation"] = self.user.occupation
        if observed.get("country_specific_first_name"):
            variables["countrySpecificFirstName"] = self.user.first_name
        if observed.get("country_specific_last_name"):
            variables["countrySpecificLastName"] = self.user.last_name
        if observed.get("identity_document_type") or observed.get("identity_document_number"):
            if document_type and document_number:
                variables["identityDocument"] = {
                    "type": document_type,
                    "value": document_number,
                }
        missing = []
        for name, required in observed.items():
            if not required:
                continue
            available = {
                "date_of_birth": bool(self.user.dob),
                "nationality": bool(nationality),
                "place_of_birth": bool(self.user.place_of_birth or nationality),
                "middle_name": bool(self.user.middle_name),
                "occupation": bool(self.user.occupation or variables.get("occupation")),
                "identity_document_type": bool(document_type),
                "identity_document_number": bool(document_number),
                "country_specific_first_name": bool(self.user.first_name),
                "country_specific_last_name": bool(self.user.last_name),
                "secondary_identity_document": False,
                "collected_consents": False,
            }.get(name, True)
            if not available:
                missing.append(name)
        if missing:
            raise RuntimeError(
                "DYNAMIC_KYC_INPUT_REQUIRED: " + ",".join(sorted(missing))
            )

    def _build_signup_variables(self, token: str) -> dict:
        card_type = self._card_issuer_type()
        address_quality = {
            "autoCompleteType": "ANS" if getattr(self, "_address_normalized_by_paypal", False) else "MANUAL",
            "isUserModified": not getattr(self, "_address_normalized_by_paypal", False),
        }
        variables = {
            "card": {
                "cardNumber": self.card.number,
                "expirationDate": self._card_expiration_date(),
                "securityCode": self.card.cvv,
                "type": card_type,
                "productClass": self.card.card_type,
            },
            "country": self.address.country,
            "email": self.user.email,
            "firstName": self.user.first_name,
            "lastName": self.user.last_name,
            "phone": {
                "countryCode": self.user.phone_country_code.lstrip("+"),
                "number": self.user.phone_local,
                "type": "MOBILE",
            },
            "supportedThreeDsExperiences": ["IFRAME"],
            "token": token,
            "billingAddress": {
                "postalCode": self.address.postal_code,
                "line1": (
                    f"PO Box {self.address.house_number}"
                    if self.country == "AE"
                    else (
                        f"{self.address.house_number} {self.address.street}"
                        if self.country in {"GB", "US", "TH", "ID", "PH", "TW", "AU", "CA", "BA", "BH"}
                        else (
                            f"{self.address.street} {self.address.house_number}"
                            if self.country in {"JP", "NL", "DE", "BA"}
                            else f"{self.address.street}, {self.address.house_number}"
                        )
                    )
                ),
                # US/GB/JP checkout variants are strict about the address
                # selected by postcode autocomplete.  Sending a synthetic
                # neighbourhood in line2 can make the selected address differ
                # from the residentialAddress object and produce a generic
                # createMemberAccount/OAS rejection.
                "line2": "" if self.country in {"GB", "US", "JP", "AU", "CA", "NL", "DE"} else self.address.district,
                "city": self.address.city,
                "state": "" if self.country in {"NL", "DE"} else self.address.state,
                "accountQuality": dict(address_quality),
                "country": self.address.country,
                "familyName": self.user.last_name,
                "givenName": self.user.first_name,
            },
            "shippingAddress": {
                "postalCode": "",
                "line1": "",
                "city": "",
                "state": "",
                "accountQuality": {
                    "autoCompleteType": "MANUAL",
                    "isUserModified": False,
                },
                "country": self.address.country,
                "familyName": self.user.last_name,
                "givenName": self.user.first_name,
            },
            "contentIdentifier": self.state.content_identifier or (
                f"{self.address.country}:{self.lang}:compliance.signupTerms"
            ),
            "marketingOptOut": False,
            "password": self.user.password,
            "dateOfBirth": self._dob_payload(),
            "identityDocument": None,
            "crsData": None,
            "legalAgreements": {},
        }
        if self.country == "BR":
            variables["identityDocument"] = {
                "type": "CPF",
                "value": self.user.cpf,
            }
        if self.country == "ID":
            # The Indonesian signup form explicitly requires National ID,
            # date of birth and Indonesian nationality.
            variables["identityDocument"] = {
                "type": self.user.identity_document_type or "NATIONAL_ID",
                "value": self.user.identity_document_number,
            }
            variables["nationality"] = self.user.nationality or "ID"
            variables["placeOfBirth"] = "ID"
        if self.country == "PH":
            # The Philippine signup form requires date of birth, nationality,
            # National ID type and ID number.
            variables["nationality"] = self.user.nationality or "PH"
            variables["identityDocument"] = {
                "type": self.user.identity_document_type or "NATIONAL_ID",
                "value": self.user.identity_document_number,
            }
        if self.country == "TW":
            variables["nationality"] = self.user.nationality or "TW"
            variables["identityDocument"] = {
                "type": self.user.identity_document_type or "NATIONAL_ID",
                "value": self.user.identity_document_number,
            }
        if self.country == "AE":
            variables["nationality"] = self.user.nationality or "AE"
            variables["identityDocument"] = {
                "type": self.user.identity_document_type or "NATIONAL_ID",
                "value": self.user.identity_document_number,
            }
        if self.country == "CA":
            # The Canadian signup UI requires an occupation selection.
            variables["occupation"] = "BUSINESS"
        if self.country == "TH":
            # Current Weasley country config declares these KYC fields for TH:
            # DateOfBirth, Nationality, IdentityDocumentType and
            # IdentityDocumentNumber.  Thai nationality restricts the document
            # type to NATIONAL_ID.  The signup-terms checkbox is client-side
            # validation and is not an empty CollectedConsent object.
            variables["nationality"] = self.user.nationality or "TH"
            variables["identityDocument"] = {
                "type": self.user.identity_document_type or "NATIONAL_ID",
                "value": self.user.identity_document_number,
            }
        self._apply_runtime_kyc_variables(variables)
        if self.country in {"GB", "US", "JP", "TH", "ID", "PH", "TW", "AE", "AU", "CA", "NL", "DE", "BA", "BH", "RO"}:
            # These onboarding variants validate a separate residentialAddress argument.
            # Supplying only billingAddress can produce RESIDENTIAL_ADDRESS_NOT_FOUND.
            variables["residentialAddress"] = {
                **variables["billingAddress"],
                "accountQuality": dict(variables["billingAddress"]["accountQuality"]),
            }
        return variables

    def _apply_normalized_address(self, normalized: dict) -> None:
        """Apply the address selected by PayPal's own postcode lookup."""
        if not isinstance(normalized, dict) or not normalized:
            return
        normalized_line1 = str(normalized.get("line1") or "").strip()
        if normalized_line1:
            if self.country in {"GB", "US", "TH", "ID", "PH", "TW", "AE", "AU", "CA", "BA", "BH"}:
                match = re.match(
                    r"^([0-9]+(?:/[0-9A-Za-z-]+)?[A-Za-z]?(?:-[0-9]+[A-Za-z]?)?)\s+(.+)$",
                    normalized_line1,
                )
                if match:
                    self.address.house_number = match.group(1)
                    self.address.street = match.group(2).strip()
                else:
                    self.address.street = normalized_line1
            elif self.country == "BR":
                normalized_line1 = re.sub(
                    r",\s*\d+[A-Za-z0-9-]*\s*$", "", normalized_line1
                ).strip()
                self.address.street = normalized_line1 or self.address.street
            elif self.country in {"NL", "DE"}:
                match = re.match(r"^(.+?)\s+(\d+[A-Za-z]?(?:-\d+)?(?:\s*[A-Za-z])?)$", normalized_line1)
                if match:
                    self.address.street = match.group(1).strip()
                    self.address.house_number = match.group(2).strip()
                else:
                    self.address.street = normalized_line1
            else:
                self.address.street = normalized_line1
        self.address.district = str(normalized.get("line2") or "").strip()
        self.address.city = normalized.get("city") or self.address.city
        self.address.state = normalized.get("state") or self.address.state
        self.address.postal_code = normalized.get("postalCode") or self.address.postal_code
        self._address_normalized_by_paypal = True

    def _normalize_address_with_paypal(self, token: str) -> None:
        # Bahrain postcode lookup may return line1 as only a building number
        # and move the road into line2. Rebuilding the payload from that shape
        # produces invalid values such as "144 3". BH only requires line1
        # and city, so retain the verified map/static address as entered.
        if self.country in {"BH", "BA"}:
            logger.info("Skipping postcode normalization for {}; preserving verified street/city fields", self.country)
            self._address_normalized_by_paypal = False
            return
        if not str(self.address.postal_code or "").strip():
            logger.info("Skipping postcode normalization: this country form has no postal code")
            return
        address_result = self.session.graphql(
            "AddressAutocompleteFromPostalCodeQuery",
            ADDRESS_AUTOCOMPLETE_FROM_POSTAL_CODE_QUERY,
            {
                "country": self.address.country,
                "postalCode": self.address.postal_code,
                "token": token,
            },
        )
        result_obj = address_result[0] if isinstance(address_result, list) else address_result
        normalized = result_obj.get("data", {}).get("addressNormalization") or {}
        if normalized:
            logger.info(
                "Address normalized by PayPal: {}, {}, {} {}",
                normalized.get("line1"),
                normalized.get("line2"),
                normalized.get("city"),
                normalized.get("state"),
            )
            self._apply_normalized_address(normalized)

    def _send_signup_attempt(self, token: str, signup_url: str) -> dict:
        card_type = self._card_issuer_type()
        try:
            self.session.graphql(
                "InstallmentOptionsQuery",
                INSTALLMENT_OPTIONS_QUERY,
                {
                    "buyerCountry": self.address.country,
                    "cardNumber": self.card.number,
                    "cardType": card_type,
                    "token": token,
                },
            )
        except Exception as e:
            logger.warning(f"InstallmentOptionsQuery failed: {e}")

        signup_fields = [
            "email",
            "phone",
            "cardNumber",
            "cardExpiry",
            "cardCvv",
            "password",
            "firstName",
            "lastName",
            "billingLine1",
            "billingCity",
            "billingPostalCode",
            "billingState",
            "dateOfBirth",
        ]
        if self.country == "BR":
            signup_fields.append("identityDocumentNumber")
        if self.country == "ID":
            signup_fields.extend(["identityDocumentType", "identityDocumentNumber", "nationality"])
        if self.country == "TH":
            signup_fields.extend(["identityDocumentType", "identityDocumentNumber", "nationality"])
        if self.country == "PH":
            signup_fields.extend(["identityDocumentType", "identityDocumentNumber", "nationality"])
        if self.country == "TW":
            signup_fields.extend(["identityDocumentType", "identityDocumentNumber", "nationality"])
        if self.country == "AE":
            signup_fields.extend(["identityDocumentType", "identityDocumentNumber", "nationality"])
        if self.country == "CA":
            signup_fields.append("occupation")
        event_names = {
            "date_of_birth": "dateOfBirth",
            "nationality": "nationality",
            "identity_document_type": "identityDocumentType",
            "identity_document_number": "identityDocumentNumber",
            "occupation": "occupation",
            "middle_name": "middleName",
            "place_of_birth": "placeOfBirth",
            "country_specific_first_name": "countrySpecificFirstName",
            "country_specific_last_name": "countrySpecificLastName",
        }
        for runtime_name in self._runtime_kyc_fields():
            event_name = event_names.get(runtime_name)
            if event_name and event_name not in signup_fields:
                signup_fields.append(event_name)
        send_signup_field_events(
            self.session,
            token,
            signup_fields,
        )
        send_weasley_log(
            self.session,
            self.state.ec_token,
            signup_url,
            [
                "weasley_create_account_and_pay_submit",
                "weasley_api_request_sign_up_new_member_mutation",
            ],
            country=self.address.country,
            lang=self.lang,
        )
        signup_result = self.session.graphql(
            "SignUpNewMemberMutation",
            SIGNUP_NEW_MEMBER_MUTATION,
            self._build_signup_variables(token),
            extra_body={"fn_sync_data": build_signup_fn_sync_data(token)},
        )
        logger.info(
            "Signup result (sanitized): {}",
            json.dumps(
                sanitize_for_log(signup_result),
                ensure_ascii=False,
                indent=2,
            )[:4000],
        )
        return signup_result

    def _consume_signup_result(self, signup_result) -> tuple[bool, list[dict]]:
        """Apply successful signup data to state. Return (success, errors)."""
        result_obj = signup_result[0] if isinstance(signup_result, list) else signup_result
        if not isinstance(result_obj, dict):
            logger.error(
                "Signup returned an unexpected response type: {}",
                type(result_obj).__name__,
            )
            return False, [{
                "message": "UNEXPECTED_RESPONSE_TYPE",
                "responseType": type(result_obj).__name__,
            }]

        data_obj = result_obj.get("data")
        data_obj = data_obj if isinstance(data_obj, dict) else {}
        onboard_data = data_obj.get("onboardAccount") or {}
        if onboard_data:
            if not isinstance(onboard_data, dict):
                onboard_data = {}
            buyer = onboard_data.get("buyer", {})
            buyer = buyer if isinstance(buyer, dict) else {}
            self.state.user_id = buyer.get("userId", "")
            auth = buyer.get("auth", {})
            auth = auth if isinstance(auth, dict) else {}
            if auth:
                self.session.set_euat_token(auth.get("accessToken", ""))
            logger.success(f"Account created! User ID: {self.state.user_id}")
            return True, []

        raw_errors = result_obj.get("errors", []) or []
        errors = [err for err in raw_errors if isinstance(err, dict)]
        if raw_errors and not errors:
            errors = [{
                "message": "UNEXPECTED_ERROR_SHAPE",
                "responseType": type(raw_errors).__name__,
            }]
        partial_access_token = self._find_access_token(errors)
        if partial_access_token:
            logger.warning(
                "Signup returned a recoverable funding-instrument contingency with an access token; "
                "the member account exists and authorization will continue"
            )
            logger.debug(
                "Recoverable signup response: {}",
                json.dumps(sanitize_for_log(result_obj), ensure_ascii=False, indent=2)[:8000],
            )
            return False, errors
        if errors:
            for err in errors:
                logger.error(
                    "Signup error detail: {}",
                    json.dumps(
                        sanitize_for_log({
                            "message": err.get("message"),
                            "name": err.get("_name"),
                            "statusCode": err.get("statusCode"),
                            "checkpoints": err.get("checkpoints"),
                            "contingency": err.get("contingency"),
                            "path": err.get("path"),
                            "data": err.get("data"),
                            "errorData": err.get("errorData"),
                            "meta": err.get("meta"),
                            "extensions": err.get("extensions"),
                        }),
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
        logger.error(
            "Signup failed because onboardAccount is empty. Sanitized response: {}",
            json.dumps(
                sanitize_for_log(result_obj),
                ensure_ascii=False,
                indent=2,
            )[:8000],
        )
        return False, errors

    @staticmethod
    def _dict_contains_card_field(value) -> bool:
        if isinstance(value, dict):
            for key, item in value.items():
                compact_key = str(key).lower().replace("_", "").replace("-", "")
                if compact_key in {"cardnumber", "card", "cardnumberfield"}:
                    return True
                if isinstance(item, str):
                    item_lower = item.lower()
                    if item_lower in {"cardnumber", "card_generic_error"}:
                        return True
                if PayPalFlow._dict_contains_card_field(item):
                    return True
        elif isinstance(value, list):
            return any(PayPalFlow._dict_contains_card_field(item) for item in value)
        return False

    @staticmethod
    def _is_card_related_signup_error(errors: list[dict]) -> bool:
        card_messages = {
            "CARD_GENERIC_ERROR",
            "INSTRUMENT_SHARING_LIMIT_EXCEEDED",
            "CC_LINKED_TO_FULL_ACCOUNT",
            "CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR",
        }
        for err in errors or []:
            if not isinstance(err, dict):
                continue
            checkpoints = set(err.get("checkpoints") or [])
            if checkpoints.intersection({"addCard", "validate.fi", "card", "fi"}):
                return True
            message = str(err.get("message") or "")
            if message in card_messages:
                return True
            # Some country onboarding backends wrap a funding-instrument
            # rejection with this generic JavaScript resolver failure instead
            # of returning the underlying card error/checkpoint. No member
            # access token is issued, so it is safe to keep the verified OTP
            # session and retry with a fresh card.
            if "Cannot destructure property 'index' of 'error' as it is undefined" in message:
                return True
            if PayPalFlow._dict_contains_card_field(err.get("errorData")):
                return True
        return False

    @staticmethod
    def _is_pre_account_card_validation_error(errors: list[dict]) -> bool:
        """Return true when PayPal rejected the card before member creation.

        `validate.fi` runs before createMemberAccount/addCard.  At this point no
        buyer account or access token exists, so replacing only the funding
        instrument does not mutate an already-created account.
        """
        retryable_messages = {
            "CC_LINKED_TO_FULL_ACCOUNT",
            "CREATE_CARD_ACCOUNT_CANDIDATE_VALIDATION_ERROR",
            "CARD_GENERIC_ERROR",
        }
        for err in errors or []:
            if not isinstance(err, dict):
                continue
            checkpoints = set(err.get("checkpoints") or [])
            message = str(err.get("message") or err.get("_name") or "")
            if "validate.fi" in checkpoints and message in retryable_messages:
                return True
        return False

    @staticmethod
    def _has_signup_error_message(errors: list[dict], message: str) -> bool:
        return any(
            isinstance(err, dict) and str(err.get("message") or "") == message
            for err in errors or []
        )

    @staticmethod
    def _funding_confirmation_context(errors: list[dict]) -> dict:
        for err in errors or []:
            if not isinstance(err, dict):
                continue
            if str(err.get("message") or "") == "FI_CONFIRMATION_CONTINGENCY":
                data = err.get("errorData") or {}
                return data if isinstance(data, dict) else {}
        return {}

    def _signup_with_card_retry(self, token: str, signup_url: str):
        """Retry SignUpNewMember with a fresh generated Visa/MasterCard on card errors."""
        self.session.set_euat_token("")
        last_errors: list[dict] = []
        last_access_token = ""

        for attempt in range(1, self.max_card_attempts + 1):
            logger.info(
                "Step 3: Creating account, signup attempt {}/{}",
                attempt,
                self.max_card_attempts,
            )
            signup_result = self._send_signup_attempt(token, signup_url)
            success, errors = self._consume_signup_result(signup_result)
            if success:
                return

            last_errors = errors
            runtime_schema = getattr(self, "runtime_form_schema", None)
            if runtime_schema:
                added_kyc = augment_dynamic_kyc_from_errors(runtime_schema, errors)
                if added_kyc:
                    logger.warning(
                        "Signup validation exposed additional KYC fields: {}",
                        ",".join(added_kyc),
                    )
                    # Validate that the current profile contains values before
                    # spending another signup attempt. The next loop rebuilds
                    # mutation variables from the augmented runtime schema.
                    self._apply_runtime_kyc_variables({})
                    continue
            access_token = self._find_access_token(errors)
            if access_token:
                last_access_token = access_token

            confirmation = self._funding_confirmation_context(errors)
            if confirmation:
                method = str(confirmation.get("confimationMethod") or confirmation.get("confirmationMethod") or "").strip()
                provider = str(confirmation.get("authenticationProvider") or "PayPal").strip()
                if access_token:
                    self.session.set_euat_token(access_token)
                    logger.warning(
                        "Ignoring FI_CONFIRMATION_CONTINGENCY as in the original flow; "
                        "member access token preserved and Hermes/authorize will continue "
                        "(provider={}, method={})",
                        provider,
                        method or "STEP_UP",
                    )
                    return
                raise RuntimeError(
                    "FUNDING_INSTRUMENT_3DS_REQUIRED_WITHOUT_EUAT: "
                    f"provider={provider}; method={method or 'STEP_UP'}"
                )

            if self._has_signup_error_message(errors, "ACCOUNT_ALREADY_EXISTS"):
                if last_access_token:
                    self.session.set_euat_token(last_access_token)
                    logger.warning(
                        "Signup returned ACCOUNT_ALREADY_EXISTS after a previous "
                        "response already issued an access token. Reusing that "
                        "token and continuing instead of re-submitting signup."
                    )
                    return
                raise RuntimeError(
                    "Signup failed: ACCOUNT_ALREADY_EXISTS and no prior access "
                    "token is available for this session."
                )

            if self._is_card_related_signup_error(errors):
                if access_token:
                    self.session.set_euat_token(access_token)
                    logger.warning(
                        "Card/addCard failed but PayPal returned an access token. "
                        "The member account is already created at this point, so "
                        "re-sending SignUpNewMember with a new card would produce "
                        "ACCOUNT_ALREADY_EXISTS. Continuing with the returned token."
                    )
                    return

                if self.country == "US" and self._is_pre_account_card_validation_error(errors):
                    if attempt >= self.max_card_attempts:
                        raise RuntimeError(
                            "US PayPal funding instrument validation failed after "
                            f"{self.max_card_attempts} card attempts"
                        )
                    message = next(
                        (
                            str(err.get("message") or err.get("_name") or "CARD_REJECTED")
                            for err in errors
                            if isinstance(err, dict)
                        ),
                        "CARD_REJECTED",
                    )
                    logger.warning(
                        "US pre-account card validation returned {}; keeping the verified "
                        "profile/address and retrying with a fresh funding instrument",
                        message,
                    )
                    self.card = generate_card(proxy_url=self.proxy_config.url)
                    self._profile_rotated()
                    continue

                if self.country == "US":
                    message = next(
                        (
                            str(err.get("message") or err.get("_name") or "CARD_REJECTED")
                            for err in errors
                            if isinstance(err, dict)
                        ),
                        "CARD_REJECTED",
                    )
                    raise RuntimeError(
                        "US PayPal signup stopped after funding-instrument validation: "
                        f"{message}. Start a clean job/session instead of changing the "
                        "funding instrument inside the verified OTP session."
                    )

                if attempt >= self.max_card_attempts:
                    raise RuntimeError(
                        "Signup failed: card was rejected after "
                        f"{self.max_card_attempts} attempts"
                    )

                card_country = str(getattr(self.address, "country", "") or "").upper()
                # AE/BH use the local unique-card generator only.
                prefer_local_card = card_country in {"AE", "BH"}
                prefer_remote_card = False
                logger.warning(
                    "Card rejected by signup/addCard. Fetching a fresh {} "
                    "Visa/MasterCard and retrying...",
                    "local unique" if prefer_local_card else "random",
                )
                self.card = generate_card(
                    proxy_url=self.proxy_config.url,
                    prefer_local=prefer_local_card,
                    prefer_remote=prefer_remote_card,
                )
                logger.info(
                    "New generated card for retry: {} exp={}",
                    self._masked_card_number(),
                    self.card.expiry,
                )
                continue

            address_not_found = self._has_signup_error_message(
                errors, "RESIDENTIAL_ADDRESS_NOT_FOUND"
            )
            if address_not_found and self.country == "US":
                raise RuntimeError(
                    "US PayPal residential address was not accepted. Start a clean job "
                    "with a consistent US proxy, phone and verified address context."
                )
            if address_not_found and attempt < self.max_card_attempts:
                previous_postcode = self.address.postal_code
                for _ in range(3):
                    candidate = generate_address(self.country)
                    if candidate.postal_code != previous_postcode:
                        self.address = candidate
                        break
                else:
                    self.address = generate_address(self.country)
                logger.warning(
                    "PayPal did not recognize the residential address; resolving another online-map {} address and retrying in the same OTP session",
                    self.country,
                )
                try:
                    self._normalize_address_with_paypal(token)
                except Exception as normalize_error:
                    logger.warning("PayPal postcode normalization retry failed: {}", normalize_error)
                self._profile_rotated()
                continue

            if access_token:
                self.session.set_euat_token(access_token)
                logger.info("Got access token from signup error response")
                return

            create_member_oas = any(
                isinstance(err, dict)
                and str(err.get("message") or err.get("_name") or "").upper() == "OAS_ERROR"
                and "createMemberAccount" in (err.get("checkpoints") or [])
                for err in errors or []
            )
            if create_member_oas:
                diagnostic = self.session.diagnostic_snapshot()
                logger.warning(
                    "OAS diagnostic: engine={} ec={} ctx={} ssrt={} content_id={} cookies={} debug_id={}",
                    diagnostic.get("engine"),
                    bool(self.state.ec_token),
                    bool(self.state.ctx_id),
                    bool(self.state.ssrt),
                    self.state.content_identifier or "<missing>",
                    ",".join(diagnostic.get("cookie_names") or []) or "<none>",
                    (diagnostic.get("last_graphql") or {}).get("paypal_debug_id") or "<missing>",
                )

            if create_member_oas and self.country == "US":
                raise RuntimeError(
                    "US PayPal createMemberAccount was rejected by onboarding risk "
                    "control (OAS_ERROR). The verified OTP session is now terminal; "
                    "start a new clean job rather than rotating profile data in place."
                )

            if create_member_oas and attempt < self.max_card_attempts:
                logger.warning(
                    "PayPal rejected the generated member profile at createMemberAccount; "
                    "rotating profile/address and retrying in the same verified phone session"
                )
                verified_phone = self.user.phone
                self.user = generate_user(verified_phone, country=self.country)
                self.address = generate_address(self.country)
                try:
                    send_tealeaf_data(self.session, signup_url)
                    send_device_fingerprint(
                        self.session,
                        token,
                        app_id="CHECKOUTUINODEWEB_ONBOARDING_LITE",
                        referer=signup_url,
                        wrapped=True,
                    )
                except Exception as warmup_error:
                    logger.debug("Profile retry warmup soft-failed: {}", warmup_error)
                self._profile_rotated()
                continue

            break

        raise RuntimeError(
            "Signup failed: no usable access token obtained. "
            f"Last errors: {json.dumps(sanitize_for_log(last_errors), ensure_ascii=False)[:1000]}"
        )

    def _profile_rotated(self) -> None:
        """Hook for web adapters to refresh masked generated-profile details."""
        return None

    def _follow_modxo_action_redirect(self, resp, referer: str):
        """Follow Next server-action redirects emitted by ModXO.

        PayPal's server action may return a normal Location header or an
        x-action-redirect header such as "/?...;push". In the latter case the
        path is relative to the /pay app, not the site root.
        """
        redirect_url = resp.headers.get("Location") or resp.headers.get("x-action-redirect") or ""
        if not redirect_url:
            return resp
        redirect_url = redirect_url.split(";", 1)[0]
        if redirect_url.startswith("/?"):
            redirect_url = f"https://www.paypal.com/pay{redirect_url}"
        elif redirect_url.startswith("/"):
            redirect_url = f"https://www.paypal.com{redirect_url}"
        logger.info(f"Following ModXO action redirect: {redirect_url[:140]}...")
        return self.session.get(
            redirect_url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer,
                "Upgrade-Insecure-Requests": "1",
            },
        )

    def _phase1_risk_controls(self):
        """Send device fingerprints, Tealeaf data, analytics."""
        logger.info("--- Phase 1: Risk control signals ---")

        # Device fingerprint (p1, p2, w endpoints)
        send_device_fingerprint(self.session, self.ba_token)

        # Tealeaf initial data
        page_url = f"https://www.paypal.com/pay?ssrt={self.state.ssrt}&token={self.ba_token}&ul=1"
        send_tealeaf_data(self.session, page_url)

        # Analytics
        send_analytics_ts(self.session, "main:xo:modxo:login", self.ba_token)
        send_observability_emit(self.session, self.ba_token)

        logger.info("Risk control signals sent")

    def _phase2_create_account(self):
        """Submit 'Create Account' action to get to the signup page."""
        logger.info("--- Phase 2: Create account flow ---")

        resp = getattr(self, "_approve_response", None) if self.state.ec_token else None
        compact_fallback_used = bool(resp is not None and self.state.ec_token)
        if resp is not None and self.state.ec_token:
            logger.info("Using EC Token from the pure-protocol Next.js/RSC bootstrap")
        pay_url = (
            f"https://www.paypal.com/pay/?ssrt={self.state.ssrt}"
            f"&token={self.ba_token}&ul=1&ctxId={self.state.ctx_id}"
            f"&country.x={self.address.country}"
        )

        # Prefer the server-provided guest onboarding route. This matches the
        # current PayPal flow and avoids depending on rotating Next-Action IDs.
        try:
            if self.state.onboarding_url:
                logger.info("Following server-provided guest onboarding route...")
                resp = self.session.get(
                    self.state.onboarding_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": pay_url,
                        "Upgrade-Insecure-Requests": "1",
                        "Sec-Fetch-Site": "same-origin",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Dest": "document",
                    },
                )
                if resp.status_code == 403:
                    manual_response = self._handle_datadome_challenge(resp, self.state.onboarding_url)
                    if manual_response is not None:
                        resp = manual_response
                if resp.status_code not in {200, 301, 302, 303, 307, 308}:
                    logger.warning("Guest onboarding route returned HTTP {}", resp.status_code)
                    resp = None
        except Exception as e:
            logger.warning("Guest onboarding route failed: {}", e)
            resp = None

        # Only crawl Next.js chunks if the server-provided onboarding route
        # failed. Normal tasks avoid those extra requests entirely.
        if resp is None and not (self.state.show_create_account_action_id and self.state.create_user_action_id):
            try:
                self._extract_modxo_action_ids(
                    getattr(self, "_approve_html", ""),
                    getattr(self, "_approve_url", pay_url),
                )
            except Exception as action_scan_error:
                logger.debug("Deferred ModXO action scan failed: {}", action_scan_error)

        # Fallback to browser-like Next server actions when the direct route is
        # unavailable but fresh action IDs were discovered.
        if resp is None and self.state.show_create_account_action_id and self.state.create_user_action_id:
            try:
                logger.info("Submitting browser-like Pay_With_Card server action...")
                pay_with_card_url = f"{pay_url}&paypal_client_cfci=modxo_vaulted_not_recurring-Pay_With_Card"
                pay_resp = self.session.post(
                    pay_with_card_url,
                    files=[
                        ("_1_ctxId", (None, self.state.ctx_id)),
                        ("_1_formName", (None, "createAccountAction")),
                        ("0", (None, '["$K1"]')),
                    ],
                    headers={
                        "Accept": "text/x-component",
                        "Origin": "https://www.paypal.com",
                        "Referer": pay_url,
                        "Next-Action": self.state.show_create_account_action_id,
                    },
                )
                if pay_resp.status_code in (301, 302, 303, 307, 308) or pay_resp.headers.get("x-action-redirect"):
                    self._follow_modxo_action_redirect(pay_resp, pay_url)

                logger.info("Submitting browser-like Continue_To_Payment server action...")
                continue_url = f"{pay_url}&paypal_client_cfci=modxo_vaulted_not_recurring-Continue_To_Payment"
                rsc_resp = self.session.post(
                    continue_url,
                    files=[
                        ("_1_ctxId", (None, self.state.ctx_id)),
                        ("_1_token", (None, self.ba_token)),
                        ("_1_login_email", (None, self.user.email)),
                        ("_1_formName", (None, "createAccount")),
                        ("0", (None, f'["$K1",{{"emailSubmitTime":{int(time.time() * 1000)}}}]')),
                    ],
                    headers={
                        "Accept": "text/x-component",
                        "Origin": "https://www.paypal.com",
                        "Referer": pay_with_card_url,
                        "Next-Action": self.state.create_user_action_id,
                    },
                )
                onboarding_url = self._extract_onboarding_redirect(rsc_resp.text)
                if onboarding_url:
                    resp = self.session.get(
                        onboarding_url,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Referer": pay_url,
                            "Upgrade-Insecure-Requests": "1",
                        },
                    )
            except Exception as e:
                logger.warning("Browser-like ModXO server-action path failed: {}", e)
                resp = None

        if resp is None:
            logger.warning("Using compact guest onboarding fallback")
            compact_fallback_used = True
            base_url = (
                f"https://www.paypal.com/pay?ssrt={self.state.ssrt}"
                f"&token={self.ba_token}&ul=1"
                f"&paypal_client_cfci=modxo_vaulted_not_recurring-Pay_With_Card"
            )
            form_data = {
                "ctxId": self.state.ctx_id,
                "formName": "createAccountAction",
                "fn_sync_data": build_fn_sync_data(self.ba_token),
            }
            resp = self.session.post(base_url, data=form_data, headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://www.paypal.com",
                "Referer": f"https://www.paypal.com/pay?ssrt={self.state.ssrt}&token={self.ba_token}&ul=1",
            })

        # Handle redirect chain
        while resp.status_code in (301, 302, 303, 307, 308):
            redirect_url = resp.headers.get("Location", "")
            if redirect_url.startswith("/"):
                redirect_url = f"https://www.paypal.com{redirect_url}"
            logger.info(f"Following redirect: {redirect_url[:100]}...")
            resp = self.session.get(redirect_url)

        html = resp.text

        # Extract EC token from the new URL or page content. PayPal currently
        # emits tokens containing characters outside ``\w`` in some routes, so
        # keep the parser aligned with the full token alphabet.
        decoded_url = urllib.parse.unquote(str(resp.url))
        decoded_html = urllib.parse.unquote(html)
        ec_match = re.search(r"(?:token=|[?&]token%3D)(EC-[A-Za-z0-9_-]+)", decoded_url, re.I)
        if ec_match:
            self.state.ec_token = ec_match.group(1)
            logger.info("EC Token: {}", sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"])
        else:
            ec_match = re.search(
                r'(?:\\?"ecToken\\?"\s*:\s*\\?"?)(EC-[A-Za-z0-9_-]+)',
                decoded_html,
                re.I,
            ) or re.search(r"EC-[A-Za-z0-9_-]+", decoded_html)
            if ec_match:
                self.state.ec_token = ec_match.group(1) if ec_match.lastindex else ec_match.group(0)
                logger.info("EC Token (from HTML): {}", sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"])

        # A server-provided onboarding URL may return a valid HTTP 200 shell
        # before the EC checkout is created. Previously that response prevented
        # the compact form fallback from running at all. Retry the compact
        # create-account route once, then re-check both redirects and content.
        if not self.state.ec_token and not compact_fallback_used:
            logger.warning("Guest onboarding page contained no EC token; retrying compact create-account route")
            compact_url = (
                f"https://www.paypal.com/pay?ssrt={self.state.ssrt}"
                f"&token={self.ba_token}&ul=1"
                f"&paypal_client_cfci=modxo_vaulted_not_recurring-Pay_With_Card"
            )
            compact_resp = self.session.post(
                compact_url,
                data={
                    "ctxId": self.state.ctx_id,
                    "formName": "createAccountAction",
                    "fn_sync_data": build_fn_sync_data(self.ba_token),
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://www.paypal.com",
                    "Referer": f"https://www.paypal.com/pay?ssrt={self.state.ssrt}&token={self.ba_token}&ul=1",
                },
            )
            while compact_resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = compact_resp.headers.get("Location", "")
                redirect_url = urllib.parse.urljoin(str(compact_resp.url), redirect_url)
                if not redirect_url:
                    break
                logger.info("Following compact onboarding redirect: {}...", redirect_url[:100])
                compact_resp = self.session.get(redirect_url)

            resp = compact_resp
            html = compact_resp.text
            decoded_url = urllib.parse.unquote(str(compact_resp.url))
            decoded_html = urllib.parse.unquote(html)
            ec_match = re.search(r"(?:token=|[?&]token%3D)(EC-[A-Za-z0-9_-]+)", decoded_url, re.I)
            if ec_match:
                self.state.ec_token = ec_match.group(1)
                logger.info("EC Token (compact retry): {}", sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"])
            else:
                ec_match = re.search(r"EC-[A-Za-z0-9_-]+", decoded_html)
                if ec_match:
                    self.state.ec_token = ec_match.group(0)
                    logger.info("EC Token (compact retry HTML): {}", sanitize_for_log({"ec_token": self.state.ec_token})["ec_token"])

        # The real browser next loads checkoutweb/weasley.  This request is not
        # just cosmetic: it sets checkout cookies (for example l7_az/x-pp-s),
        # exposes the current content hash, and matches the Referer/context
        # expected by the following GraphQL mutations.
        if self.state.ec_token:
            signup_url = self._build_signup_url()
            self.state.signup_url = signup_url
            logger.info(f"Loading checkout signup app: {signup_url}")
            signup_resp = self.session.get(
                signup_url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": str(resp.url),
                    "Upgrade-Insecure-Requests": "1",
                },
            )
            logger.info(
                "Checkout signup app loaded: {} bytes={}",
                signup_resp.status_code,
                len(signup_resp.content),
            )
            if signup_resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = signup_resp.headers.get("Location", "")
                if redirect_url:
                    redirect_url = urllib.parse.urljoin(signup_url, redirect_url)
                    if "/checkoutweb/signup" in redirect_url:
                        self.state.signup_url = redirect_url
                    logger.warning(
                        "Checkout signup app redirected to {}; preserving signup referer {}",
                        redirect_url[:140],
                        self.state.signup_url[:140],
                    )
            self._signup_html = signup_resp.text
            self._signup_html = signup_resp.text
            initial_data = self._extract_window_initial_data(signup_resp.text)
            content_hash = initial_data.get("contentHash")
            if content_hash:
                self.state.content_hash = content_hash
                logger.info(f"Content hash: {self.state.content_hash}")
            content_identifier = self._extract_content_identifier(
                signup_resp.text,
                self.address.country,
                self.lang,
            )
            if content_hash and content_identifier.endswith(":compliance.signupTerms") and content_hash not in content_identifier:
                content_identifier = f"{self.address.country}:{self.lang}:{content_hash}:compliance.signupTerms"
            self.state.content_identifier = content_identifier
            logger.info(f"Content identifier: {self.state.content_identifier}")

        # Send Tealeaf for new page
        send_tealeaf_data(
            self.session,
            self.state.signup_url if self.state.signup_url else str(resp.url),
        )
        send_observability_emit(self.session, self.ba_token)

        if self.state.ec_token:
            # Browser trace sends signup-page Weasley logs and EC-token risk
            # beacons before phone/card submission.  Missing these correlates
            # with opaque OAS_ERROR/createMemberAccount buckets.
            send_weasley_log(
                self.session,
                self.state.ec_token,
                self.state.signup_url,
                [
                    "weasley_client_eligibility_check_success",
                    "WEASLEY_PAGE_INTERACTIVE_FPTI",
                    "WEASLEY_PREPARE_BILLING_PAGE_FPTI",
                    "weasley_payment_request_api_available",
                ],
                country=self.address.country,
                lang=self.lang,
            )
            send_device_fingerprint(
                self.session,
                self.state.ec_token,
                app_id="CHECKOUTUINODEWEB_ONBOARDING_LITE",
                referer=self.state.signup_url,
                wrapped=True,
            )

        # Send the initial GraphQL queries
        logger.info("Sending checkout session GraphQL queries...")
        try:
            self.session.graphql(
                "DeferredFeature",
                DEFERRED_FEATURE_QUERY,
                {
                    "channel": "WEB",
                    "countryCodeAsString": self.address.country,
                    "integrationType": "XoSignupAuth",
                    "isBaslAsString": "false",
                    "isForcedGuest": "false",
                    "token": self.state.ec_token or self.ba_token,
                },
            )
        except Exception as e:
            logger.warning(f"DeferredFeature failed: {e}")

        try:
            self.session.graphql(
                "CheckoutSessionDataQuery",
                CHECKOUT_SESSION_DATA_QUERY,
                {"token": self.state.ec_token or self.ba_token},
            )
        except Exception as e:
            logger.warning(f"CheckoutSessionDataQuery failed: {e}")

        try:
            self.session.graphql(
                "GriffinMetadataQuery",
                GRIFFIN_METADATA_QUERY,
                {
                    "countryCode": self.address.country,
                    "languageCode": self.lang,
                    "shippingCountryCode": self.address.country,
                },
            )
        except Exception as e:
            logger.warning(f"GriffinMetadataQuery failed: {e}")

        try:
            self.session.graphql(
                "SupportedFundingSourcesQuery",
                SUPPORTED_FUNDING_SOURCES_QUERY,
                {
                    "token": self.state.ec_token or self.ba_token,
                    "userCountry": self.address.country,
                },
            )
        except Exception as e:
            logger.warning(f"SupportedFundingSourcesQuery failed: {e}")

        try:
            self._normalize_address_with_paypal(self.state.ec_token or self.ba_token)
        except Exception as e:
            logger.warning(f"AddressAutocompleteFromPostalCodeQuery failed: {e}")

    def _phase3_signup_and_2fa(self):
        """Submit the signup form and trigger 2FA SMS.

        Actual flow discovered from traffic capture:
        1. InitiateRiskBasedTwoFactorPhoneConfirmationMutation → sends SMS, returns authId + challengeId
        2. ConfirmRiskBasedTwoFactorPhoneConfirmationMutation → verifies OTP pin with authId + challengeId
        3. SignUpNewMemberMutation → creates account with all user data + card + address
        """
        logger.info("--- Phase 3: Signup form + 2FA ---")

        # Send Tealeaf to simulate form interaction
        signup_url = self.state.signup_url or "https://www.paypal.com/checkoutweb/signup"
        send_tealeaf_data(self.session, signup_url)

        token = self.state.ec_token or self.ba_token

        # Step 1/2: Send SMS and confirm OTP. If the OTP is wrong, the
        # operator can either retry a code for the same phone or enter a new
        # phone number to trigger a fresh challenge.
        self._confirm_phone_with_retry(token, signup_url)

        # Step 3: Sign up new member with all user data. If PayPal rejects the
        # card at addCard/validate.fi/cardNumber, fetch a new generated
        # Visa/MasterCard and submit SignUpNewMember again.
        self._signup_with_card_retry(token, signup_url)

        if not self.state.euat_token:
            raise RuntimeError(
                "Signup failed: no access token obtained. "
                "Cannot proceed to authorization without authentication."
            )

        self.session.set_euat_token(self.state.euat_token)

        # Send analytics for signup completion
        send_analytics_ts(
            self.session,
            "main:billing:hagrid:billingwithoutpurchase:member:review",
            self.ba_token,
            ec_token=self.state.ec_token,
            user_id=self.state.user_id,
        )

    def _sync_buyer_context(self, token: str, referer: str) -> bool:
        """Refresh the checkout buyer context after signup and before authorize."""
        if not self.state.euat_token:
            logger.warning("Buyer-context sync skipped because the signup access token is missing")
            return False

        self.session.set_euat_token(self.state.euat_token)

        try:
            result = self.session.graphql(
                "BuyerContextQuery",
                BUYER_CONTEXT_QUERY,
                {"token": token},
                extra_headers={
                    "Referer": referer,
                    "X-App-Name": "checkoutuinodeweb",
                    "X-PayPal-Internal-EUAT": self.state.euat_token,
                    "PayPal-Client-Context": token,
                    "PayPal-Client-Metadata-Id": self.state.paypal_client_metadata_id,
                    "X-Country": self.address.country,
                    "X-Locale": self.locale,
                },
                endpoint="https://www.paypal.com/graphql/",
            )
        except Exception as sync_error:
            logger.warning("Buyer-context GraphQL refresh failed: {}", sync_error)
            return False

        result_obj = result[0] if isinstance(result, list) and result else result
        if not isinstance(result_obj, dict):
            logger.warning("Buyer-context refresh returned type {}", type(result_obj).__name__)
            return False
        data_obj = result_obj.get("data")
        data_obj = data_obj if isinstance(data_obj, dict) else {}
        checkout = data_obj.get("checkoutSession")
        checkout = checkout if isinstance(checkout, dict) else {}
        buyer = checkout.get("buyer")
        buyer = buyer if isinstance(buyer, dict) else {}
        auth = buyer.get("auth")
        auth = auth if isinstance(auth, dict) else {}
        refreshed_access_token = auth.get("accessToken")
        if isinstance(refreshed_access_token, str) and refreshed_access_token:
            self.session.set_euat_token(refreshed_access_token)
        user_id = buyer.get("userId")
        if isinstance(user_id, str) and user_id:
            self.state.user_id = user_id
            logger.info("Buyer context synchronized for user {}", user_id)
            return True
        logger.warning("Buyer-context refresh completed without a buyer userId")
        return False

    def _load_review_context(self, url: str, referer: str, max_redirects: int = 8):
        """Load the full Hermes/Hagrid redirect chain and preserve the final session cookies."""
        current = url
        current_referer = referer
        response = None
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }
        for _ in range(max_redirects + 1):
            response = self.session.get(current, headers={**headers, "Referer": current_referer})
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location", "")
            if not location:
                return response
            next_url = urllib.parse.urljoin(current, location)
            current_referer, current = current, next_url
        return response

    def _before_final_authorize(self, review_url: str) -> None:
        """Optional web-adapter hook before the final authorization mutation."""
        return None


    def _phase4_authorize(self) -> dict:
        """Send the final authorize mutation to approve the billing agreement."""
        logger.info("--- Phase 4: Final authorization ---")

        hermes_base_url = (
            f"https://www.paypal.com/webapps/hermes?"
            f"ssrt={self.state.ssrt}&ul=1&modxo_redirect_reason=guest_user"
            f"&locale.x={self.locale}&country.x={self.address.country}"
            f"&ba_token={self.ba_token}&token={self.state.ec_token}"
            f"&rcache=1&cookieBannerVariant=hidden&fromSignupLite=true"
            f"&fallback=1&reason=Q0FSRF9HRU5FUklDX0VSUk9S"
        )
        hermes_contingency_url = (
            f"https://www.paypal.com/webapps/hermes?"
            f"ssrt={self.state.ssrt}&ul=1&modxo_redirect_reason=guest_user"
            f"&locale.x={self.locale}&country.x={self.address.country}"
            f"&ba_token={self.ba_token}&token={self.state.ec_token}"
            f"&rcache=1&cookieBannerVariant=hidden&fromSignupLite=true"
            f"&addFIContingency=noretry&redirectToHermes=true"
            f"&fallback=1&reason=Q0FSRF9HRU5FUklDX0VSUk9S"
        )
        review_referer = f"{hermes_base_url}&billingLite=1"
        review_url = f"{review_referer}#/billingweb/review"

        # Load the exact billingLite review context and follow the complete
        # redirect chain so EUAT, buyer and checkout cookies belong to one session.
        try:
            logger.info("Loading Hermes/Hagrid review context...")
            self._load_review_context(hermes_contingency_url, self.state.signup_url)
            review_resp = self._load_review_context(review_referer, hermes_contingency_url)
            logger.info(
                "Hermes/Hagrid review loaded: {} bytes={}",
                review_resp.status_code,
                len(review_resp.content),
            )
            if not self.state.user_id:
                user_match = re.search(r'(?:party_id|cust|userId)["=:]+([A-Z0-9]{8,20})', review_resp.text)
                if user_match:
                    self.state.user_id = user_match.group(1)
                    logger.info(f"User ID from Hermes page: {self.state.user_id}")
        except Exception as e:
            logger.warning(f"Loading Hermes/Hagrid review context failed: {e}")

        self._before_final_authorize(review_url)
        self.session.set_euat_token(self.state.euat_token)
        try:
            self._load_review_context(review_referer, hermes_base_url)
        except Exception as e:
            logger.warning(f"Reloading review context after browser synchronization failed: {e}")

        # Send Tealeaf for the review page
        send_tealeaf_data(self.session, review_url)

        # The critical authorize mutation
        billing_agreement_id = self.state.ec_token or self.ba_token
        buyer_synced = self._sync_buyer_context(billing_agreement_id, review_referer)
        if getattr(self, "_strict_buyer_context", False) and not buyer_synced:
            raise RuntimeError(
                "BUYER_CONTEXT_SYNC_FAILED: member identity is not attached to the billing checkout"
            )
        logger.info(
            "Authorizing billing agreement: {}",
            sanitize_for_log({"billingAgreementId": billing_agreement_id})["billingAgreementId"],
        )

        def send_authorize(*, include_client_context: bool = False):
            extra_headers = {
                "Referer": review_referer,
                "X-App-Name": "checkoutuinodeweb",
                "PayPal-Client-Metadata-Id": self.state.paypal_client_metadata_id,
            }
            if include_client_context:
                extra_headers.update({
                    "PayPal-Client-Context": billing_agreement_id,
                    "X-Country": self.address.country,
                    "X-Locale": self.locale,
                })
            else:
                extra_headers.update({
                    "PayPal-Client-Context": None,
                    "X-Country": None,
                    "X-Locale": None,
                })
            return self.session.graphql(
                "authorize",
                AUTHORIZE_BILLING_MUTATION,
                {
                    "billingAgreementId": billing_agreement_id,
                    "fundingPreference": {
                        "balancePreference": "OPT_OUT",
                    },
                    "legalAgreements": {},
                },
                extra_headers=extra_headers,
                batched=True,
                endpoint="https://www.paypal.com/graphql/",
            )

        result = send_authorize()
        for buyer_retry in range(1, 3):
            if not self._has_buyer_not_set(result):
                break
            logger.warning(
                "authorize returned BUYER_NOT_SET; rebuilding buyer/review context (attempt {}/2)...",
                buyer_retry,
            )
            try:
                self._load_review_context(review_referer, hermes_base_url)
                self.session.set_euat_token(self.state.euat_token)
                buyer_synced = self._sync_buyer_context(billing_agreement_id, review_referer)
                if getattr(self, "_strict_buyer_context", False) and not buyer_synced:
                    raise RuntimeError("BUYER_CONTEXT_SYNC_FAILED during BUYER_NOT_SET recovery")
                time.sleep(float(buyer_retry))
            except Exception as e:
                logger.warning(f"Reloading billingLite review context failed: {e}")
            result = send_authorize(include_client_context=True)

        logger.info(
            "Authorization result (sanitized): {}",
            json.dumps(sanitize_for_log(result), ensure_ascii=False, indent=2)[:1000],
        )

        # Extract return URL and user ID from response
        try:
            result_obj = result[0] if isinstance(result, list) else result
            data_obj = result_obj.get("data") if isinstance(result_obj, dict) else None
            data_obj = data_obj if isinstance(data_obj, dict) else {}
            billing_data = data_obj.get("billing", {})
            auth_data = billing_data.get("authorize") if isinstance(billing_data, dict) else None
            if not isinstance(auth_data, dict):
                errors = result_obj.get("errors") if isinstance(result_obj, dict) else None
                error_code = "BUYER_NOT_SET" if self._has_buyer_not_set(result) else "AUTHORIZE_EMPTY"
                logger.error(
                    "Authorization failed: authorize is empty. Errors: {}",
                    json.dumps(sanitize_for_log(errors or []), ensure_ascii=False, indent=2),
                )
                return {
                    "status": "error",
                    "error_code": error_code,
                    "error": "authorize returned empty result",
                    "stage": "final_authorization",
                    "retryable": error_code == "BUYER_NOT_SET",
                    "raw_response": result,
                }
            self.state.return_url = auth_data["returnURL"]["href"]
            self.state.user_id = auth_data["buyer"]["userId"]
            ba_token_resp = auth_data["billingAgreementToken"]

            logger.success(
                "Billing Agreement Token: {}",
                sanitize_for_log({"billingAgreementToken": ba_token_resp})["billingAgreementToken"],
            )
            logger.success(f"Payment Action: {auth_data['paymentAction']}")
            logger.success(f"Buyer User ID: {self.state.user_id}")
            logger.success("Return URL: <redacted>")

            final_redirect_url = ""
            try:
                logger.info("Following merchant return URL...")
                return_resp = self.session.get(
                    self.state.return_url,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": review_url,
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                for _ in range(8):
                    if return_resp.status_code not in (301, 302, 303, 307, 308):
                        break
                    location = return_resp.headers.get("Location", "")
                    if not location:
                        break
                    final_redirect_url = urllib.parse.urljoin(str(return_resp.url), location)
                    return_resp = self.session.get(
                        final_redirect_url,
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Referer": str(return_resp.url),
                            "Upgrade-Insecure-Requests": "1",
                        },
                    )
                if not final_redirect_url:
                    final_redirect_url = str(return_resp.url)
                logger.success("Final merchant URL: <redacted>")
            except Exception as e:
                logger.warning(f"Following merchant return URL failed: {e}")

            redirect_status = ""
            verification_url = ""
            pending_url = ""
            settlement_status = "authorization_only"
            if final_redirect_url:
                try:
                    final_parsed = urllib.parse.urlparse(final_redirect_url)
                    final_query = urllib.parse.parse_qs(final_parsed.query)
                    redirect_status = str((final_query.get("redirect_status") or [""])[0]).lower()
                    verification_url = str((final_query.get("success_return_url") or [""])[0])
                    if (
                        not verification_url
                        and (final_parsed.hostname or "").lower() == "chatgpt.com"
                        and final_parsed.path.startswith("/checkout/verify")
                    ):
                        verification_url = final_redirect_url
                    if redirect_status in {"success", "succeeded"}:
                        settlement_status = "confirmed"
                    elif redirect_status in {"pending", "processing", "requires_action"}:
                        settlement_status = "pending_verification"
                    elif verification_url:
                        settlement_status = "pending_verification"
                except Exception as status_error:
                    logger.warning("Parsing merchant completion status failed: {}", status_error)
            for candidate_url in (verification_url, final_redirect_url):
                try:
                    candidate = urllib.parse.urlparse(str(candidate_url or ""))
                    candidate_host = (candidate.hostname or "").lower()
                    if candidate.scheme == "https" and candidate_host in {
                        "chatgpt.com",
                        "chat.openai.com",
                        "pay.openai.com",
                    }:
                        pending_url = str(candidate_url)
                        break
                except Exception:
                    continue
            if settlement_status == "pending_verification":
                logger.warning(
                    "PayPal agreement is authorized, but OpenAI/Stripe still requires checkout verification"
                )

            # Send final analytics
            send_analytics_ts(
                self.session,
                "main:billing:hagrid:billingwithoutpurchase:member:submitButtonFullEvent",
                self.ba_token,
                ec_token=self.state.ec_token,
                user_id=self.state.user_id,
                event="cl",
            )

            return {
                "status": "success",
                "ba_token": ba_token_resp,
                "ec_token": self.state.ec_token,
                "user_id": self.state.user_id,
                "return_url": self.state.return_url,
                "final_redirect_url": final_redirect_url,
                "verification_url": verification_url,
                "pending_url": pending_url,
                "redirect_status": redirect_status,
                "settlement_status": settlement_status,
                "payment_action": auth_data["paymentAction"],
            }
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Failed to parse authorization response: {e}")
            return {
                "status": "error",
                "error": str(e),
                "raw_response": result,
            }
