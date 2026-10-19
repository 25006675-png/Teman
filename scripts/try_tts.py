import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # the app's modules
import tts  # noqa: E402

voice = sys.argv[1] if len(sys.argv) > 1 else tts.DEFAULT_VOICE
result = tts.make_speech("阿婆，你今日可以用二十五令吉。", voice=voice)
print(result)
