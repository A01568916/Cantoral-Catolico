#!/usr/bin/env python3
"""
agregar_canto.py — Agrega cantos al cantoral RiseUp desde imagen o PDF.

Uso:
    python agregar_canto.py foto_canto.jpg
    python agregar_canto.py canto.pdf
    python agregar_canto.py --manual     # modo manual: tecleas el canto

Requisitos (instalar una sola vez):
    pip install pytesseract pillow pdf2image requests python-dotenv
    # En Mac: brew install tesseract poppler
    # En Windows: instalar Tesseract desde https://github.com/UB-Mannheim/tesseract/wiki
                   instalar poppler desde https://github.com/oschwartz10612/poppler-windows/releases
"""

import sys
import os
import re
import json
import base64
import datetime
import argparse
import textwrap
from pathlib import Path

# ── Carga configuración desde .env ──────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # .env es opcional

GITHUB_TOKEN  = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO   = os.getenv("GITHUB_REPO", "")   # e.g. "usuario/cantoral-riseup"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
SONGS_PATH    = os.getenv("SONGS_PATH", "songs.json")  # ruta dentro del repo


# ════════════════════════════════════════════════════════════════════
#  PARTE 1: OCR — Extraer texto de imagen o PDF
# ════════════════════════════════════════════════════════════════════

def extract_text_from_image(path: str) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        fatal("Falta pytesseract o Pillow. Ejecuta: pip install pytesseract pillow")

    img = Image.open(path)
    # Aumentar resolución para mejor OCR
    if img.width < 1200:
        scale = 1200 / img.width
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    text = pytesseract.image_to_string(img, lang="spa+eng")
    return text


def extract_text_from_pdf(path: str) -> str:
    try:
        from pdf2image import convert_from_path
    except ImportError:
        fatal("Falta pdf2image. Ejecuta: pip install pdf2image")

    pages = convert_from_path(path, dpi=300)
    texts = []
    for page in pages:
        texts.append(extract_text_from_image_obj(page))
    return "\n".join(texts)


def extract_text_from_image_obj(img) -> str:
    try:
        import pytesseract
    except ImportError:
        fatal("Falta pytesseract. Ejecuta: pip install pytesseract")
    return pytesseract.image_to_string(img, lang="spa+eng")


def extract_text(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        print(f"📄 Leyendo PDF: {path}")
        return extract_text_from_pdf(path)
    elif ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"):
        print(f"🖼  Leyendo imagen: {path}")
        return extract_text_from_image(path)
    else:
        fatal(f"Formato no soportado: {ext}. Usa jpg, png, pdf.")


# ════════════════════════════════════════════════════════════════════
#  PARTE 2: Parser — Convertir texto OCR al formato [Acorde]letra
# ════════════════════════════════════════════════════════════════════

# Notas válidas
NOTES = ["C#", "Db", "D#", "Eb", "F#", "Gb", "G#", "Ab", "A#", "Bb",
         "C", "D", "E", "F", "G", "A", "B"]

# Regex para detectar si una palabra es un acorde
CHORD_WORD_RE = re.compile(
    r'^([A-G][b#]?)'
    r'((?:maj|min|m|sus|add|aug|dim|M)?[0-9]{0,2}'
    r'(?:maj|min|m|sus|add|aug|dim)?[0-9]{0,2})'
    r'((?:/[A-G][b#]?)?)$'
)

INVALID_CHORDS = {"E#", "B#", "C##", "D##", "F##", "G##", "A##"}

ENHARMONIC_FIX = {
    "E#": "F", "B#": "C",
    "C##": "D", "D##": "E", "F##": "G", "G##": "A", "A##": "B"
}


def is_chord_token(token: str) -> bool:
    """¿Es este token un acorde válido?"""
    t = token.strip("(),.-")
    m = CHORD_WORD_RE.match(t)
    if not m:
        return False
    root = m.group(1) + (m.group(0)[len(m.group(1))] if len(t) > len(m.group(1)) and t[len(m.group(1))] in "b#" else "")
    # Verificar que la raíz es nota real (ya cubierto por el regex)
    return True


def is_chord_line(line: str) -> bool:
    """Una línea es de acordes si ≥85% de sus tokens son acordes."""
    stripped = line.strip()
    if not stripped:
        return False
    tokens = stripped.split()
    if not tokens:
        return False
    # Quitar separadores comunes
    tokens = [t for t in tokens if t not in ("-", "–", "|", "/", "x2", "x3", "x4", "(x2)", "(x3)", "(x4)")]
    if not tokens:
        return True  # línea solo con separadores = línea de acordes
    chord_count = sum(1 for t in tokens if is_chord_token(t))
    ratio = chord_count / len(tokens)
    return ratio >= 0.8


def fix_chord(chord: str) -> str:
    """Corrige acordes inválidos (E#→F, etc.)."""
    return ENHARMONIC_FIX.get(chord, chord)


def parse_raw_text(raw: str) -> list[dict]:
    """
    Convierte texto OCR (estilo 'acordes encima de letra') al formato
    de líneas con [Acorde] incrustado.

    Devuelve lista de dicts: {"type": "section"|"chordlyric"|"lyric"|"empty", "text": str}
    """
    lines = raw.splitlines()
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Línea vacía
        if not stripped:
            result.append({"type": "empty", "text": ""})
            i += 1
            continue

        # Sección [Verso], [Coro], etc.
        if re.match(r'^\[.+\]$', stripped) or re.match(r'^\(.+\)$', stripped):
            label = stripped.strip("[]()").strip()
            result.append({"type": "section", "text": label})
            i += 1
            continue

        # ¿Línea de acordes? Mirar si la siguiente es letra
        if is_chord_line(line):
            chord_line = line
            lyric_line = None

            # La siguiente línea es letra si no es acordes ni vacía ni sección
            if (i + 1 < len(lines)
                    and lines[i + 1].strip()
                    and not is_chord_line(lines[i + 1])
                    and not re.match(r'^\[.+\]$', lines[i + 1].strip())):
                lyric_line = lines[i + 1]
                i += 2
            else:
                i += 1

            if lyric_line is not None:
                combined = merge_chords_into_lyric(chord_line, lyric_line)
                result.append({"type": "chordlyric", "text": combined})
            else:
                # Solo acordes, sin letra
                chords = " ".join(chord_line.split())
                result.append({"type": "chordlyric", "text": chords})
            continue

        # Línea de letra pura
        result.append({"type": "lyric", "text": stripped})
        i += 1

    return result


def merge_chords_into_lyric(chord_line: str, lyric_line: str) -> str:
    """
    Fusiona una línea de acordes y su letra en el formato [Acorde]letra.

    Ejemplo:
      chord_line: "       Dm                A#"
      lyric_line: "Por tu iglesia que te espera"
      resultado:  "[Dm]Por tu iglesia que te [A#]espera"
    """
    # Encontrar posición y texto de cada acorde en la línea de acordes
    chord_positions = []
    for m in re.finditer(r'\S+', chord_line):
        token = m.group()
        if is_chord_token(token):
            chord_positions.append((m.start(), fix_chord(token)))

    if not chord_positions:
        return lyric_line.strip()

    lyric = lyric_line  # mantener espacios originales para posicionamiento

    # Construir resultado insertando marcadores
    result = []
    prev_pos = 0

    for chord_pos, chord in chord_positions:
        # Posición en la letra: tomar el índice tal cual (las columnas deberían coincidir)
        # Si el índice supera la longitud de la letra, agregar al final
        insert_at = min(chord_pos, len(lyric))

        # Extraer la parte de letra antes de este acorde
        segment = lyric[prev_pos:insert_at]
        result.append(segment)
        result.append(f"[{chord}]")
        prev_pos = insert_at

    # Resto de la letra
    result.append(lyric[prev_pos:])

    return "".join(result).strip()


def build_content_string(parsed_lines: list[dict]) -> str:
    """Convierte las líneas parseadas en el string de contenido del canto."""
    parts = []
    for item in parsed_lines:
        if item["type"] == "empty":
            parts.append("")
        elif item["type"] == "section":
            parts.append(f"[{item['text']}]")
        elif item["type"] in ("chordlyric", "lyric"):
            parts.append(item["text"])
    return "\n".join(parts)


# ════════════════════════════════════════════════════════════════════
#  PARTE 3: Interacción con el usuario para completar metadatos
# ════════════════════════════════════════════════════════════════════

def ask(prompt: str, default: str = "") -> str:
    val = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return val if val else default


def edit_in_editor(content: str) -> str:
    """Abre el contenido en el editor del sistema para revisión."""
    import tempfile
    import subprocess

    editor = os.environ.get("EDITOR", "nano" if sys.platform != "win32" else "notepad")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(content)
        tmp_path = f.name

    subprocess.run([editor, tmp_path])

    with open(tmp_path, encoding="utf-8") as f:
        edited = f.read()
    os.unlink(tmp_path)
    return edited


def collect_song_metadata(suggested_content: str) -> dict:
    """Pide al usuario los metadatos del canto y permite editar el contenido."""
    print("\n" + "─" * 60)
    print("📝 METADATOS DEL CANTO")
    print("─" * 60)

    title  = ask("Título del canto")
    artist = ask("Artista / Intérprete", "")
    key    = ask("Tono original (ej: G, Am, Bb)")
    capo   = ask("Cejilla (ej: 'Cejilla 2', dejar vacío si no hay)", "")

    print("\n" + "─" * 60)
    print("📄 CONTENIDO DETECTADO (primeras 30 líneas):")
    print("─" * 60)
    for line in suggested_content.splitlines()[:30]:
        print(f"  {line}")
    if len(suggested_content.splitlines()) > 30:
        print(f"  ... ({len(suggested_content.splitlines())} líneas en total)")

    print("\n¿Qué deseas hacer con el contenido?")
    print("  1. Usar como está")
    print("  2. Editar en editor de texto")
    print("  3. Reemplazar manualmente (escribir en terminal)")
    choice = ask("Opción", "1")

    if choice == "2":
        suggested_content = edit_in_editor(suggested_content)
        print("✅ Contenido actualizado desde el editor.")
    elif choice == "3":
        print("Escribe el contenido del canto. Escribe '###' en una línea vacía para terminar:")
        lines = []
        while True:
            l = input()
            if l.strip() == "###":
                break
            lines.append(l)
        suggested_content = "\n".join(lines)
        print("✅ Contenido reemplazado.")

    return {
        "title":   title,
        "artist":  artist,
        "key":     key,
        "capo":    capo,
        "content": suggested_content.strip()
    }


# ════════════════════════════════════════════════════════════════════
#  PARTE 4: GitHub — Leer y escribir songs.json
# ════════════════════════════════════════════════════════════════════

def github_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }


def get_songs_from_github() -> tuple[list, str]:
    """Descarga songs.json del repo. Devuelve (lista_cantos, sha_del_archivo)."""
    import urllib.request
    import urllib.error

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{SONGS_PATH}?ref={GITHUB_BRANCH}"
    req = urllib.request.Request(url, headers=github_headers())
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        content = base64.b64decode(data["content"]).decode("utf-8")
        sha = data["sha"]
        songs = json.loads(content)
        return songs, sha
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # El archivo no existe todavía, empezar vacío
            return [], ""
        raise


def push_songs_to_github(songs: list, sha: str, commit_message: str):
    """Sube la lista de cantos a songs.json en GitHub."""
    import urllib.request

    content_bytes = json.dumps(songs, ensure_ascii=False, indent=2).encode("utf-8")
    encoded = base64.b64encode(content_bytes).decode("utf-8")

    payload = {
        "message": commit_message,
        "content": encoded,
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{SONGS_PATH}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=github_headers(), method="PUT")

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ════════════════════════════════════════════════════════════════════
#  PARTE 5: Validación de acordes
# ════════════════════════════════════════════════════════════════════

CHORD_RE_INLINE = re.compile(r'\[([A-G][b#]?[^\]]*)\]')

VALID_NOTES_SET = {
    "C", "C#", "Db",
    "D", "D#", "Eb",
    "E",
    "F", "F#", "Gb",
    "G", "G#", "Ab",
    "A", "A#", "Bb",
    "B"
}


def validate_chords_in_content(content: str) -> list[str]:
    """
    Revisa el contenido en formato [Acorde]letra y devuelve una lista de
    problemas encontrados.
    """
    problems = []
    for i, line in enumerate(content.splitlines(), 1):
        for m in CHORD_RE_INLINE.finditer(line):
            chord_text = m.group(1)
            # Extraer la nota raíz
            note_match = re.match(r'^([A-G][b#]?)', chord_text)
            if not note_match:
                problems.append(f"Línea {i}: acorde no reconocido [{chord_text}]")
                continue
            root = note_match.group(1)
            if root not in VALID_NOTES_SET:
                problems.append(f"Línea {i}: nota inválida '{root}' en [{chord_text}]")

            # Revisar bajo de acorde (ej: /E#)
            bass_match = re.search(r'/([A-G][b#]?)', chord_text)
            if bass_match:
                bass = bass_match.group(1)
                if bass not in VALID_NOTES_SET:
                    problems.append(f"Línea {i}: bajo inválido '/{bass}' en [{chord_text}]")

    return problems


# ════════════════════════════════════════════════════════════════════
#  PARTE 6: Modo manual (sin imagen)
# ════════════════════════════════════════════════════════════════════

MANUAL_HELP = """
FORMATO ACEPTADO:
─────────────────
Opción A (acordes en línea separada, como aparecen en PDFs):
       Dm                A#
Por tu iglesia que te espera

Opción B (acordes incrustados con corchetes):
[Dm]Por tu iglesia que te [A#]espera

Secciones: [Verso 1]  [Coro]  [Puente]  etc.
Líneas solo con acordes (intros, instrumentales): Dm A# F Am
"""


def manual_entry() -> str:
    print(MANUAL_HELP)
    print("Escribe el contenido del canto. Línea '###' sola para terminar:\n")
    lines = []
    while True:
        line = input()
        if line.strip() == "###":
            break
        lines.append(line)
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════
#  UTILIDADES
# ════════════════════════════════════════════════════════════════════

def fatal(msg: str):
    print(f"\n❌ Error: {msg}")
    sys.exit(1)


def check_config():
    if not GITHUB_TOKEN:
        fatal(
            "No está configurado GITHUB_TOKEN.\n"
            "Crea un archivo .env en la misma carpeta con:\n"
            "  GITHUB_TOKEN=tu_token_aqui\n"
            "  GITHUB_REPO=usuario/nombre-del-repo\n"
            "Guía para crear el token: https://github.com/settings/tokens\n"
            "(Permisos necesarios: repo → contents → write)"
        )
    if not GITHUB_REPO:
        fatal(
            "No está configurado GITHUB_REPO en el archivo .env.\n"
            "Ejemplo: GITHUB_REPO=juanperez/cantoral-riseup"
        )


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Agrega un canto al cantoral RiseUp desde imagen, PDF o texto manual.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
            Ejemplos:
              python agregar_canto.py foto.jpg
              python agregar_canto.py canto.pdf
              python agregar_canto.py --manual
        """)
    )
    parser.add_argument("archivo", nargs="?", help="Imagen o PDF del canto")
    parser.add_argument("--manual", action="store_true", help="Ingresar el canto manualmente")
    parser.add_argument("--dry-run", action="store_true", help="No subir a GitHub, solo mostrar resultado")
    args = parser.parse_args()

    if not args.manual and not args.archivo:
        parser.print_help()
        sys.exit(0)

    # ── Paso 1: Obtener texto ────────────────────────────────────────
    if args.manual:
        raw_text = manual_entry()
    else:
        if not Path(args.archivo).exists():
            fatal(f"No se encontró el archivo: {args.archivo}")
        raw_text = extract_text(args.archivo)

    # ── Paso 2: Parsear texto al formato [Acorde]letra ───────────────
    print("\n🔍 Analizando acordes y letra...")
    parsed = parse_raw_text(raw_text)
    suggested_content = build_content_string(parsed)

    # ── Paso 3: Pedir metadatos y permitir edición ───────────────────
    song = collect_song_metadata(suggested_content)

    # ── Paso 4: Validar acordes ──────────────────────────────────────
    problems = validate_chords_in_content(song["content"])
    if problems:
        print("\n⚠️  Se detectaron posibles problemas con los acordes:")
        for p in problems:
            print(f"  • {p}")
        cont = ask("\n¿Continuar de todas formas? (s/n)", "n")
        if cont.lower() != "s":
            print("Operación cancelada. Edita el contenido y vuelve a intentar.")
            sys.exit(0)

    # ── Paso 5: Mostrar resumen ──────────────────────────────────────
    print("\n" + "═" * 60)
    print("✅ RESUMEN DEL CANTO")
    print("═" * 60)
    print(f"  Título:  {song['title']}")
    print(f"  Artista: {song['artist'] or '—'}")
    print(f"  Tono:    {song['key']}")
    print(f"  Cejilla: {song['capo'] or 'ninguna'}")
    print(f"  Líneas:  {len(song['content'].splitlines())}")
    print()

    if args.dry_run:
        print("─── DRY RUN: contenido que se subiría ───")
        print(json.dumps(song, ensure_ascii=False, indent=2))
        print("─── Fin dry run ───")
        sys.exit(0)

    confirm = ask("¿Subir al cantoral en GitHub? (s/n)", "s")
    if confirm.lower() != "s":
        print("Cancelado.")
        sys.exit(0)

    # ── Paso 6: Subir a GitHub ───────────────────────────────────────
    check_config()
    print("\n📡 Conectando con GitHub...")

    try:
        songs, sha = get_songs_from_github()
    except Exception as e:
        fatal(f"No pude leer songs.json de GitHub: {e}")

    songs.append(song)

    commit_msg = f"Agregar canto: {song['title']}"
    try:
        push_songs_to_github(songs, sha, commit_msg)
    except Exception as e:
        fatal(f"No pude subir el archivo a GitHub: {e}")

    print(f"\n🎉 ¡Listo! '{song['title']}' fue agregado al cantoral.")
    print(f"   Los usuarios verán el canto nuevo al recargar la página.")


if __name__ == "__main__":
    main()
