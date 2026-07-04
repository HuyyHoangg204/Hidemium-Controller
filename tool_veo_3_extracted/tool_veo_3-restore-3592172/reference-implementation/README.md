# Reference Implementation

This folder contains the canonical Python runtime plus a simple desktop GUI for the Banana Python integration docs.

Purpose:

- give developers a working source baseline
- give AI agents concrete code to copy instead of inferring behavior from prose
- eliminate ambiguity around browser bootstrap, XHR create-image, and Python-side polling

Files:

- [banana_client.py](./banana_client.py)
- [browser_scripts.py](./browser_scripts.py)
- [scheduler.py](./scheduler.py)
- [gui_app.py](./gui_app.py)
- [run_app.py](./run_app.py)
- [requirements.txt](./requirements.txt)

What it does:

- accepts one or more user-provided access tokens
- launches system Google Chrome
- attaches over CDP
- warms the Flow page
- gets a reCAPTCHA token inside the page
- sends the create-image request via browser-context XHR
- polls async status in Python
- downloads the final image in Python
- uses one shared Chrome / Flow runtime
- runs all Playwright / page work on one dedicated browser thread
- schedules multiple prompts in parallel using logical worker lanes
- `thread_count` controls how many jobs may run at once
- tokens are assigned to jobs in round-robin order
- create-image requests are dispatched concurrently through the shared browser runtime
- polling and file download continue in parallel on Python worker threads
- the shared Chrome profile is reused across normal runs
- the profile is deleted only when the runtime performs a hard browser reset
- supports both `image` mode and `video` mode
- video mode requires one reference image path

Run:

```powershell
cd <this-folder>
.\run.bat
```

Important:

- this reference implementation is the code companion to the docs
- `run.bat` launches the GUI app
- `run_app.py` remains available as the CLI fallback
- if prose and code appear to disagree, update the docs to match the working source-of-truth before building a new integration
- the machine still needs system Google Chrome installed
- token input format:
  - one token per line
- prompt input format:
  - prompts separated by blank lines
