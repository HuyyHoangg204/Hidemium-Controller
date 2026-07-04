import os
import sys

if getattr(sys, "frozen", False):
    ROOT_DIR = os.path.dirname(sys.executable)
else:
    _DIR = os.path.dirname(os.path.abspath(__file__))
    ROOT_DIR = os.path.abspath(os.path.join(_DIR, ".."))

CHROME_PROFILES_DIR = os.path.join(ROOT_DIR, "chrome_profiles")
CAPTCHA_WORKER_PROFILE = os.path.join(CHROME_PROFILES_DIR, "captcha_worker")
