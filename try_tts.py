import sys
import tts

voice = sys.argv[1] if len(sys.argv) > 1 else tts.DEFAULT_VOICE
result = tts.make_speech("阿婆，你今日可以用二十五令吉。", voice=voice)
print(result)