import PyInstaller.__main__
import os

# Base parameters
args = [
    "app.py",
    "--name=AutoVoice_Veo3",
    "--windowed",  # Hide console window
    "--noconfirm",
    "--clean",
    "--onefile",  # Pakcage everything into a single .exe
    # Folders to include
    "--add-data=core;core",
    "--add-data=ui;ui",
    "--add-data=workers;workers",
    # Critical files
    "--add-data=browser_config.json;.",
    # Hidden imports for dynamic modules
    "--hidden-import=pymongo",
    "--hidden-import=requests",
    "--hidden-import=PySide6",
    "--hidden-import=curl_cffi",
    "--hidden-import=curl_cffi.requests",
    # Icon (optional, use if exists)
]

# Run PyInstaller
print(">>> Running PyInstaller with args:", args)
PyInstaller.__main__.run(args)
print(">>> Build Complete! Check the 'dist' folder.")
