import json
import os
import genanki
import re
from bs4 import BeautifulSoup

import spacy
# Load spaCy model
nlp_spaCy = spacy.load("en_core_web_lg", disable=["ner"])
nlp_spaCy.add_pipe("sentencizer", before="parser")
nlp_spaCy.get_pipe("lemmatizer").lookups

#https://github.com/stanfordnlp/stanza/tree/main
import stanza
stanza.download("en")
# =========================
# Load Stanza pipeline
# =========================
nlp_stanza = stanza.Pipeline(
    lang="en",
    processors="tokenize,pos,lemma",
    use_gpu=False
)

# Unique IDs - Keep these constant to allow future updates
MODEL_ID = 1782130091
# Base ID for the main deck
BASE_DECK_ID = 2026062215

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
#rubric {
  text-align: center;
  padding: 4px;
  padding-left: 10px;
  padding-right: 10px;
  margin-bottom: 10px;
  background: #1d6695;
  color: white;
  font-weight: 500;
}
/* Night Mode adjustments */
.nightMode {
    background-color: #121212 !important;
    color: #e0e0e0 !important;
}

.nightMode .word { color: #ffffff !important; }
.nightMode .pron { color: #cccccc !important; }
.nightMode .example { color: #a0cfff !important; }
.nightMode .def { color: #dddddd !important; }
.nightMode .hint { color: #999999 !important; }

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
    color: #cc3e50;
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
    "4000 EEW (Old Books) Model",
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
            "qfmt": '<div id="rubric">4000 EEW (Old Books)</div>'
            '<div class="word">{{Word}}</div>'
            '<div class="pron">{{Pronunciation}}</div>'
            '<hr>'
            '<div class="hint">{{hint:Hint}}</div>',

            "afmt": '{{FrontSide}}<hr id="answer">'
            '<br>{{Image}}'
            '<div class="def">{{Definition}}</div>'
            '<hr>'
            '<div class="exam">{{Example}}</div>'
            '<hr>{{Audio}}',
        }
    ],
    css=STYLE
)

def extract_text_from_html(html):
    """
    Remove HTML tags but keep the text content.
    """
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ")

def clean_text(html_text):
    text = extract_text_from_html(html_text)

    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("’", "'")

    text = re.sub(r"\s+", " ", text)

    text = re.sub(r"\s+\.", ".", text)
    text = re.sub(r"\s+!", "!", text)
    text = re.sub(r"\s+\?", "?", text)

    return text.strip()

#spaCy
def find_sentence_with_word_spaCy(html_story, target_word):
    """
    Find the first sentence containing the target word
    using lemma-based matching.
    """

    # Extract plain text
    text = clean_text(html_story)

    # Process with spaCy
    doc = nlp_spaCy(text)

    target_lemma = list(nlp_spaCy(target_word))[0].lemma_.lower()

    print("target_word:", target_word)
    print("target_lemma:", target_lemma)

    # Iterate sentences
    for sent in doc.sents:
        print("target_word:", target_word)
        print("target_lemma:", target_lemma)
        print("SENT:", sent.text)
        for token in sent:
            print("  TOKEN:", token.text, token.lemma_)
            if (
                token.text.lower() == target_word.lower()
                or token.lemma_.lower() == target_lemma
            ):
                return sent.text.strip()
 
    return find_sentence_with_word_Stanza(html_story, target_word)

#Stanza
def find_sentence_with_word_Stanza(html_story, target_word):
    """
    Find the first sentence containing the target word
    using lemma-based matching.
    """

    # Extract plain text
    text = clean_text(html_story)

    # Process with Stanza
    doc = nlp_stanza(text)

    nlp_target = nlp_stanza(target_word)
    target_lemma = nlp_target.sentences[0].words[0].lemma.lower()

    print("target_word:", target_word)
    print("target_lemma:", target_lemma)

    # Iterate sentences
    for sent in doc.sentences:
        print("target_word:", target_word)
        print("target_lemma:", target_lemma)
        print("SENT:", sent.text)
        for token in sent.words:
            print("  TOKEN:", token.text, token.lemma)
            if (
                token.text.lower() == target_word.lower()
                or token.lemma.lower() == target_lemma
            ):
                return sent.text.strip()
 
    print("Context not found.")
    #return None
    return "Context not found."

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

def create_deck(book_id):
    """
    book_id: integer from 1 to 6
    book_folder: path to the folder containing data.json, images, and audio
    """

    print(f"--Book: {book_id}")

    book_folder = f"output/book{book_id}"

    # Hierarchical deck naming: Parent::Child
    deck_name = f"4000 EEW (Old Books)::Book {book_id}"
    
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
        
        unit_tag = unit["en"].replace(" ", "")
        print(f"----Unit: {unit["en"]}")
        
        #if "Unit2" != unit_tag: continue

        story_text = unit['reading'][0].get("story", "")

        for entry in unit["wordlist"]:
            word = entry.get("en", "")

            #if word != "scare": continue

            pron = entry.get("pron", "")
            definition = entry.get("desc", "")
            example = entry.get("exam", "")
            image_name = entry.get("image", "")
            audio_name = entry.get("sound", "")
            # Extract the sentence from the story text using the helper function
            #hint_sentence = get_sentence_with_word(story_text, word)
            hint_sentence = find_sentence_with_word_spaCy(story_text, word)

            #print(f"Hint: {hint_sentence}")

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
                tags=[
                    f"Book{book_id}",
                    f"{unit_tag}",
                    f"Book{book_id}::{unit_tag}"
                ]
            )
            deck.add_note(note)
            #if word == "scare": break
        #break
    package = genanki.Package(deck)
    package.media_files = media_files
    output_file = f"4000EEW_Old_Book_{book_id}.apkg"
    package.write_to_file(output_file)
    print(f"Generated: {output_file}")
    
if __name__ == "__main__":
    for i in range(1,7):
        create_deck(i)
