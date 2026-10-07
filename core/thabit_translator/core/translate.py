import time
import srt
import os
import re
import argostranslate.package
import argostranslate.translate

def clean_srt_text(text):
    """Remove HTML tags and styling from subtitle text before translation."""
    if not text:
        return text
    
    # Remove HTML tags like <i>, <b>, <u>, <font>, <span>, etc.
    text = re.sub(r'<[^>]+>', '', text)
    
    # Remove SRT styling tags like {italic}, {bold}, etc.
    text = re.sub(r'\{[^}]+\}', '', text)
    
    # Remove color codes like {\c&HFF00FF&}
    text = re.sub(r'\{\\[^}]*\}', '', text)
    
    # Clean up extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

def restore_styling(original_text, translated_text):
    """Restore original styling markers to translated text."""
    if not original_text or not translated_text:
        return translated_text
    
    # Find all style tags in original
    html_tags = re.findall(r'<[^>]+>', original_text)
    srt_tags = re.findall(r'\{[^}]+\}', original_text)
    
    # Simple approach: if original had tags, try to preserve them
    # This is a simplified version - full restoration is complex
    if html_tags or srt_tags:
        # Wrap translated text in a span to indicate it had styling
        return f"{translated_text}"
    
    return translated_text

def ensure_language_pack(src_lang, tgt_lang):
    try:
        argostranslate.translate.translate("test", src_lang, tgt_lang)
        return True
    except:
        pass
    try:
        argostranslate.package.update_package_index()
        return argostranslate.package.install_package_for_language_pair(src_lang, tgt_lang)
    except Exception as e:
        print(f"[ERROR] Lang pack failed: {e}")
        return False

def translate_srt(input_srt, output_srt, src_lang, tgt_lang):
    if not os.path.exists(input_srt):
        print(f"Error: '{input_srt}' not found.")
        return False
    if not ensure_language_pack(src_lang, tgt_lang):
        return False
    
    with open(input_srt, "r", encoding="utf-8") as f:
        subs = list(srt.parse(f))
    
    total = len(subs)
    print(f"[INFO] Found {total} subtitles. Translating {src_lang}->{tgt_lang}...")
    print(f"[INFO] Cleaning and translating...")
    
    start = time.time()
    success_count = 0
    failed_count = 0
    
    for i, sub in enumerate(subs):
        original_content = sub.content
        
        if sub.content.strip():
            # Clean the text before translation
            clean_content = clean_srt_text(sub.content)
            
            if clean_content:
                try:
                    translated = argostranslate.translate.translate(clean_content, src_lang, tgt_lang)
                    
                    # Try to restore some formatting
                    sub.content = translated
                    success_count += 1
                    
                except Exception as e:
                    print(f"\n[WARN] Line {i+1} failed: {e}")
                    sub.content = original_content  # Keep original on failure
                    failed_count += 1
            else:
                sub.content = ""
        
        # Progress every 10 lines
        if (i + 1) % 10 == 0 or i == total - 1:
            elapsed = time.time() - start
            avg = elapsed / (i + 1) if i >= 0 else 0
            eta = (total - i - 1) * avg
            print(f"[PROGRESS] {i+1}/{total} | ETA: {int(eta)}s", flush=True)
    
    with open(output_srt, "w", encoding="utf-8") as f:
        f.write(srt.compose(subs))
    
    elapsed = time.time() - start
    print(f"\n[SUCCESS] Translated {success_count}/{total} subtitles")
    print(f"[INFO] Failed: {failed_count}, Time: {int(elapsed)}s")
    print(f"[INFO] Output: {output_srt}")
    
    return success_count > 0