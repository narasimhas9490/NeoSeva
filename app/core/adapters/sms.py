import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("neoseva.sms")


class SmsSendError(Exception):
    pass


class ConsoleSms:
    def __init__(self, cfg):
        """Keep the configuration for the console provider.
        Nothing is sent anywhere; the code goes to the server log.
        Used whenever SMS_ENABLED is false."""
        self.cfg = cfg

    def send_otp(self, phone, code, language):
        """Pretend to send a login code by writing it to the log.
        The code is printed only when SMS_LOG_OTP is true.
        Never raises, so development sign-in always works."""
        if self.cfg.sms_log_otp:
            log.warning("OTP for %s (%s): %s", phone, language, code)
        else:
            log.info("OTP issued for %s (%s)", phone, language)


class TwoFactorSms:
    def __init__(self, cfg):
        """Keep the configuration for 2Factor.in.
        The API key, template names and timeout come from the env.
        Templates are chosen by the caller's language."""
        self.cfg = cfg

    def send_otp(self, phone, code, language):
        """Send a login code through 2Factor.in's OTP endpoint.
        The Telugu template is used for te, English otherwise.
        Raises SmsSendError on any transport or vendor failure."""
        template = self.cfg.twofactor_template_login_te if language == "te" else self.cfg.twofactor_template_login_en
        parts = [self.cfg.twofactor_base_url.rstrip("/"), self.cfg.twofactor_api_key, "SMS", phone, code]
        if template:
            parts.append(template)
        url = "/".join(urllib.parse.quote(p, safe=":/+") for p in parts)
        try:
            with urllib.request.urlopen(url, timeout=self.cfg.twofactor_timeout_seconds) as resp:
                payload = json.loads(resp.read().decode() or "{}")
        except Exception as exc:
            raise SmsSendError(str(exc)) from exc
        if payload.get("Status") != "Success":
            raise SmsSendError(payload.get("Details") or "vendor refused the message")


def sms_provider(cfg):
    """Pick the SMS provider from configuration.
    With SMS_ENABLED false the console provider is always used.
    SMS_PROVIDER=twofactor selects 2Factor.in."""
    if not cfg.sms_enabled or cfg.sms_provider == "console":
        return ConsoleSms(cfg)
    if cfg.sms_provider == "twofactor":
        return TwoFactorSms(cfg)
    raise RuntimeError(f"unknown SMS_PROVIDER {cfg.sms_provider}")
