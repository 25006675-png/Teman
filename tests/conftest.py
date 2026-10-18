import os

# Tests never call the real Gemini API; AI tests mock it explicitly.
os.environ["DIALECTGUARD_AI"] = "off"
