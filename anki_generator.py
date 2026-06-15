import json
import os
import genanki
import re

# Unique IDs - Keep these constant to allow future updates
MODEL_ID = 1607392319
# Base ID for the main deck
BASE_DECK_ID = 2059400811

# CSS for centering content and basic styling
STYLE = """
.card {
    font-family: "Segoe UI", Arial, sans-serif;
    font-size: 22px;
    text-align: center;
    color: #222222;
    background-color: #f7f7f7;
    line-height: 1.6;
    padding: 20px;
}

.word {
    font-size: 36px;
    font-weight: bold;
    color: #111111;
}

.pron {
    font-size: 22px;
    color: #555555;
}

.exam {
    font-size: 22px;
    color: #2c3e50;
    margin-top: 10px;
}

.def {
    font-size: 20px;
    color: #333333;
}

img {
    max-width: 320px;
    height: auto;
    display: block;
    margin: 15px auto;
}

.hint a {
    color: #1a73e8;
    font-size: 18px;
}
"""


my_model = genanki.Model(
    MODEL_ID,
    "4000 Essential English Words Model",
    fields=[
        {"name": "Word"},
        {"name": "Pronunciation"},
        {"name": "Definition"},
        {"name": "Hint"},       # Field for the contextual sentence from the story
        {"name": "Example"},
        {"name": "Image"},
        {"name": "Audio"},
    ],
    templates=[
        {
            "name": "Card 1",
            "qfmt": '<div class="word">{{Word}}</div>'
            '<div class="pron">{{Pronunciation}}</div>'
            '<br>'
            '<div class="hint">{{hint:Hint}}</div>',

            "afmt": '{{FrontSide}}<hr id="answer">'
            '<br>{{Image}}'
            '<div class="def">{{Definition}}</div>'
            '<br>'
            '<div class="exam">{{Example}}</div>'
            '<br>{{Audio}}',
        }
    ],
    css=STYLE
)

def get_sentence_with_word(story_text, word):
    """
    Extracts the full sentence containing the specific word from the story text.
    Preserves existing HTML tags (like <strong>) within the sentence.
    """
    # Regex pattern: capture text between punctuation marks that includes the target word
    pattern = rf'([^.!?]*\b{re.escape(word)}\b[^.!?]*[.!?])'
    match = re.search(pattern, story_text, re.IGNORECASE)
    
    # Return the matched sentence or a fallback if not found
    return match.group(1).strip() if match else "Context not found."

def create_deck(book_id, book_folder):
    """
    book_id: integer from 1 to 6
    book_folder: path to the folder containing data.json, images, and audio
    """
    # Hierarchical deck naming: Parent::Child
    deck_name = f"4000 Essential English Words (Old Books)::Book {book_id}"
    
    # Generate a unique ID for each sub-deck based on the book number
    current_deck_id = BASE_DECK_ID + book_id
    deck = genanki.Deck(current_deck_id, deck_name)

    data_path = os.path.join(book_folder, "data.json")
    with open(data_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    media_files = []

    for unit in data["flashcard"]:
        if "wordlist" not in unit:
            continue

        story_text = unit['reading'][0].get("story", "")

        for entry in unit["wordlist"]:
            word = entry.get("en", "")
            pron = entry.get("pron", "")
            definition = entry.get("desc", "")
            example = entry.get("exam", "")
            image_name = entry.get("image", "")
            audio_name = entry.get("sound", "")
            # Extract the sentence from the story text using the helper function
            hint_sentence = get_sentence_with_word(story_text, word)
            print(f"Hint: {hint_sentence}")
            image_path = os.path.join(book_folder, "images", image_name)
            audio_path = os.path.join(book_folder, "audio", audio_name)

            if os.path.exists(image_path):
                media_files.append(image_path)
            if os.path.exists(audio_path):
                media_files.append(audio_path)

            note = genanki.Note(
                model=my_model,
                fields=[
                    word,
                    pron,
                    definition,
                    hint_sentence,
                    example,
                    f'<img src="{image_name}">',
                    f'[sound:{audio_name}]'
                ],
            )
            deck.add_note(note)
            #break
        #break
    package = genanki.Package(deck)
    package.media_files = media_files
    output_file = f"4000_Essential_English_Words_Book_{book_id}.apkg"
    package.write_to_file(output_file)
    print(f"Generated: {output_file}")
    
if __name__ == "__main__":
    # Example for Book 1
    create_deck(1, "output/book1")
