const myHeaders = new Headers();
myHeaders.append('accept', '*/*');
myHeaders.append('accept-language', 'en-US,en;q=0.9');
myHeaders.append(
  'authorization',
  'Bearer ya29.a0AUMWg_JVtiJMkh8yu_VDe1ikVhoOjQ1hIGQ59y7M6eGaE1cFQPJVN1d4-Ygs7gwILNbNogpfYhdCndOkYyBdHrlkuwq18To39OheNI8xRWLkJCa3zwHLX8Uqjet4mg8LaPsh42IsItHIRTUTCK89fJPOcRtj8mC39acuR8S19Ztl3XNqEBnlt4NotUWg5JhQdgzi6yS5qEWYCTMyajI6iKNbDxm3YPM5MXhsGqLpp-DiwE_j1PCFtaf10Fj3iEOlzfSTjVuoXlzEqrOqzloRQo5PwLOQ0-ZPSruaYyopPOWeLpb-lP86fS2siIIm_g4Y8bcBorq2xVty9gNLPhf-vcBh4UeNXTO3i6qDAzbCUgaCgYKAb8SARMSFQHGX2Mi6Q0fa2EEaN3tV_WEtk5bWg0369',
);
myHeaders.append('content-type', 'text/plain;charset=UTF-8');
myHeaders.append('origin', 'https://labs.google');
myHeaders.append('priority', 'u=1, i');
myHeaders.append('referer', 'https://labs.google/');
myHeaders.append(
  'sec-ch-ua',
  '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
);
myHeaders.append('sec-ch-ua-mobile', '?0');
myHeaders.append('sec-ch-ua-platform', '"Linux"');
myHeaders.append('sec-fetch-dest', 'empty');
myHeaders.append('sec-fetch-mode', 'cors');
myHeaders.append('sec-fetch-site', 'cross-site');
myHeaders.append(
  'user-agent',
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
);
myHeaders.append('x-browser-channel', 'stable');
myHeaders.append(
  'x-browser-copyright',
  'Copyright 2025 Google LLC. All rights reserved.',
);
myHeaders.append('x-browser-validation', 'GmxFHkay2DZYmUuquumNHEHyU78=');
myHeaders.append('x-browser-year', '2025');
myHeaders.append(
  'x-client-data',
  'CIa2yQEIorbJAQipncoBCKKDywEIlaHLAQiHoM0BCJWkzwEY1KTPAQ==',
);

const total = 100;
let successCount = 0;
let failureCount = 0;

(async () => {
  for (let i = 0; i < 100; i++) {
    await new Promise((resolve) => setTimeout(resolve, 1000));

    const response = await fetch('http://localhost:3000/captcha');
    const res = await response.json();
    const captcha = res.captcha;

    const raw =
      '{\n    "clientContext": {\n        "recaptchaToken": "' +
      captcha +
      '",\n        "sessionId": ";1768446743085",\n        "projectId": "7b0e3167-1e71-4be6-a4b1-b234980a3a34",\n        "tool": "PINHOLE",\n        "userPaygateTier": "PAYGATE_TIER_TWO"\n    },\n    "requests": [\n        {\n            "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",\n            "seed": 6738,\n            "textInput": {\n                "prompt": "con trau danh nhau"\n            },\n            "videoModelKey": "veo_3_1_t2v_fast_ultra",\n            "metadata": {\n                "sceneId": "6da7d1bd-eb7c-4cc1-989d-8c66493b43d1"\n            }\n        }\n    ]\n}';

    const requestOptions = {
      method: 'POST',
      headers: myHeaders,
      body: raw,
      redirect: 'follow',
    };

    fetch(
      'https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText',
      requestOptions,
    )
      .then((response) => response.json())
      .then((result) => {
        console.log(result);
        successCount++;
      })
      .catch((error) => {
        console.error(error);
        failureCount++;
      });
  }
  console.log(
    `Success: ${successCount}/${total}, Failure: ${failureCount}/${total}`,
  );
})();
