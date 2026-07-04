from textwrap import dedent

RECAPTCHA_READY_JS = dedent("""
() => !!(window.grecaptcha && window.grecaptcha.enterprise && window.grecaptcha.enterprise.execute)
""").strip()

EXECUTE_IMAGE_JS = dedent("""
async (config) => {
  try {
    const captchaToken = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Captcha timeout (30s)')), 30000);
      if (typeof grecaptcha === 'undefined' || !grecaptcha.enterprise) {
        clearTimeout(timeout);
        reject(new Error('reCAPTCHA library not loaded'));
        return;
      }
      grecaptcha.enterprise.ready(async () => {
        try {
          const token = await grecaptcha.enterprise.execute(config.siteKey, { action: config.action });
          clearTimeout(timeout);
          resolve(token);
        } catch (e) {
          clearTimeout(timeout);
          reject(e);
        }
      });
    });

    const payload = JSON.parse(config.payloadJson);

    if (payload.clientContext) {
      payload.clientContext.recaptchaContext = {
        token: captchaToken,
        applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB'
      };
    }

    if (payload.requests && Array.isArray(payload.requests)) {
      payload.requests.forEach((req) => {
        if (req.clientContext) {
          req.clientContext.recaptchaContext = {
            token: captchaToken,
            applicationType: 'RECAPTCHA_APPLICATION_TYPE_WEB'
          };
        }
      });
    }

    const response = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(config.method || 'POST', config.apiUrl, true);
      xhr.setRequestHeader('Authorization', 'Bearer ' + config.bearerToken);
      xhr.setRequestHeader('Content-Type', 'text/plain;charset=UTF-8');
      xhr.onreadystatechange = function () {
        if (xhr.readyState === 4) {
          let data = null;
          try {
            data = JSON.parse(xhr.responseText);
          } catch (e) {
            data = { rawText: xhr.responseText };
          }
          resolve({ ok: xhr.status >= 200 && xhr.status < 300, status: xhr.status, data: data });
        }
      };
      xhr.onerror = () => reject(new Error('Network error (XHR Failed)'));
      xhr.send(JSON.stringify(payload));
    });

    return response;
  } catch (e) {
    return { ok: false, status: 0, error: e.message };
  }
}
""").strip()
