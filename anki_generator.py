import json
import os
import genanki

# Unique IDs - Keep these constant to allow future updates
MODEL_ID = 1607392319
# Base ID for the main deck
BASE_DECK_ID = 2059400811

# CSS for centering content and basic styling
STYLE = """
.card {
    font-family: arial;
    font-size: 20px;
    text-align: center;
    color: black;
    background-color: white;
}
img {
    max-width: 300px;
    height: auto;
    display: block;
    margin: 10px auto;
}
"""

my_model = genanki.Model(
    MODEL_ID,
    "4000 Essential English Words Model",
    fields=[
        {"name": "Word"},
        {"name": "Pronunciation"},
        {"name": "Definition"},
        {"name": "Example"},
        {"name": "Image"},
        {"name": "Audio"},
    ],
    templates=[
        {
            "name": "Card 1",
            "qfmt": '<div class="word">{{Word}}</div><div class="pron">{{Pronunciation}}</div><br>{{hint:Image}}',
            "afmt": '{{FrontSide}}<hr id="answer"><div class="def">{{Definition}}</div><br><div class="exam">{{Example}}</div><br>{{Audio}}',
        }
    ],
    css=STYLE
)

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
            
        for entry in unit["wordlist"]:
            word = entry.get("en", "")
            pron = entry.get("pron", "")
            definition = entry.get("desc", "")
            example = entry.get("exam", "")
            image_name = entry.get("image", "")
            audio_name = entry.get("sound", "")

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
                    example,
                    f'<img src="{image_name}">',
                    f'[sound:{audio_name}]'
                ],
            )
            deck.add_note(note)

    package = genanki.Package(deck)
    package.media_files = media_files
    output_file = f"4000_Essential_English_Words_Book_{book_id}.apkg"
    package.write_to_file(output_file)
    print(f"Generated: {output_file}")

if __name__ == "__main__":
    # Example for Book 1
    create_deck(1, "output/book1")
