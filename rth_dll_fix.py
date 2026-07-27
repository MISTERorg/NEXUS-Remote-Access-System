import os
import sys

if sys.platform == "win32":
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass and os.path.exists(meipass):
        try:
            os.add_dll_directory(meipass)
        except Exception:
            pass
        os.environ["PATH"] = meipass + os.pathsep + os.environ.get("PATH", "")
