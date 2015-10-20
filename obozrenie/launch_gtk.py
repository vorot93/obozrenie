import os

from obozrenie.global_settings import *

from obozrenie.gtk import *

os.setpgrp()  # create new process group, become its leader
try:
    core_instance = core.Core()
    settings_instance = core.Settings(core_instance, os.path.expanduser(PROFILE_PATH))
    app_instance = App(core_instance, settings_instance)
    app_instance.run(None)
except Exception as e:
    print(e)
finally:
    os.killpg(0, signal.SIGTERM)  # kill all processes in my group
