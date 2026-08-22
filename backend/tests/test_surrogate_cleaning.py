import sys
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).parent.parent.parent))

from backend.services.chat_file_parser import clean_surrogates

def test_clean_surrogates_basic():
    # 1. Clean string with standard emoji and valid characters
    s = "Hello World! \U0001f40d"
    assert clean_surrogates(s) == s

def test_clean_surrogates_with_lone_surrogate():
    # 2. Lone surrogate gets replaced
    s = "Hello \ud83d World"
    # standard python encode-replace inserts '?' for encoding errors
    assert clean_surrogates(s) == "Hello ? World"

def test_clean_surrogates_with_unpaired_split_pair():
    # 3. Two surrogate characters representing a valid pair get correctly paired up!
    s = "Pair: \ud83d\udc0d"
    assert clean_surrogates(s) == "Pair: \U0001f40d"

def test_clean_surrogates_recursive_dict():
    # 4. Cleans dictionaries recursively
    payload = {
        "text": "lone \ud83d",
        "emoji": "pair \ud83d\udc0d",
        "nested": {
            "more": "lone \ud83d"
        },
        "list": ["lone \ud83d", 123]
    }
    cleaned = clean_surrogates(payload)
    assert cleaned["text"] == "lone ?"
    assert cleaned["emoji"] == "pair \U0001f40d"
    assert cleaned["nested"]["more"] == "lone ?"
    assert cleaned["list"] == ["lone ?", 123]

if __name__ == "__main__":
    test_clean_surrogates_basic()
    test_clean_surrogates_with_lone_surrogate()
    test_clean_surrogates_with_unpaired_split_pair()
    test_clean_surrogates_recursive_dict()
    print("All surrogate cleaning tests passed successfully!")
