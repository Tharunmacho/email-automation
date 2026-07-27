import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from app.extraction.text_extractor import extract_text

def main():
    if len(sys.argv) < 2:
        print("Usage: python test_ocr.py <path_to_image_or_pdf>")
        sys.exit(1)
        
    file_path = Path(sys.argv[1])
    if not file_path.exists():
        print(f"Error: File not found at {file_path}")
        sys.exit(1)
        
    print(f"Reading file: {file_path.name}")
    data = file_path.read_bytes()
    
    print("Running text extraction...")
    try:
        result = extract_text(data, file_path.name)
        print("\n--- EXTRACTION SUCCESS ---")
        print(f"Method: {result.method}")
        print(f"OCR Used: {result.ocr_used}")
        print(f"Char Count: {result.char_count}")
        
        output_file = file_path.with_name(f"{file_path.stem}_extracted.txt")
        output_file.write_text(result.text, encoding="utf-8")
        print(f"Extracted text written successfully to: {output_file}")
        
        print("\n--- FIRST 500 CHARACTERS ---")
        print(result.text[:500].encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8'))
    except Exception as e:
        print(f"\nExtraction failed: {e}")

if __name__ == "__main__":
    main()
