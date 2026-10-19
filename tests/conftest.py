import os

# Tests never call the real Gemini API; AI tests mock it explicitly.
os.environ["DIALECTGUARD_AI"] = "off"
# Tests never load the 3 GB speech model at startup.
os.environ["DIALECTGUARD_WHISPER_PRELOAD"] = "off"
# Tests run saved-bill payments explicitly, never on a background timer.
os.environ["DIALECTGUARD_SCHEDULER"] = "off"
