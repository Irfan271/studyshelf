import hashlib,socket,struct,zipfile
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image,UnidentifiedImageError
ALLOWED_EXTENSIONS={".pdf",".doc",".docx",".ppt",".pptx",".xls",".xlsx",".csv",".txt",".rtf",".odt",".ods",".odp",".md"}
SIGNATURES={".pdf":(b"%PDF-",),".doc":(bytes.fromhex("D0CF11E0A1B11AE1"),),".xls":(bytes.fromhex("D0CF11E0A1B11AE1"),),".ppt":(bytes.fromhex("D0CF11E0A1B11AE1"),),".docx":(b"PK\x03\x04",),".xlsx":(b"PK\x03\x04",),".pptx":(b"PK\x03\x04",),".odt":(b"PK\x03\x04",),".ods":(b"PK\x03\x04",),".odp":(b"PK\x03\x04",),".rtf":(b"{\\rtf",)}
TEXT_EXTENSIONS={".csv",".txt",".md"}
def validate_note_file(upload):
    ext=Path(upload.name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS: raise ValidationError("Unsupported file type. Allowed: PDF, DOC/DOCX, PPT/PPTX, XLS/XLSX, CSV, TXT, RTF, ODT/ODS/ODP and MD.")
    if upload.size<=0 or upload.size>settings.MAX_NOTE_UPLOAD_BYTES: raise ValidationError("File must be non-empty and no larger than 10 MB.")
    head=upload.read(16); upload.seek(0)
    if ext in SIGNATURES and not any(head.startswith(sig) for sig in SIGNATURES[ext]): raise ValidationError("File content does not match its extension.")
    if ext in {".docx",".xlsx",".pptx",".odt",".ods",".odp"}:
        try:
            with zipfile.ZipFile(upload) as archive:
                names=set(archive.namelist())
                required={".docx":"word/document.xml",".xlsx":"xl/workbook.xml",".pptx":"ppt/presentation.xml"}
                if ext in required and required[ext] not in names:
                    raise ValidationError("The ZIP container is not a valid Office document of this type.")
                if ext in {".odt",".ods",".odp"} and "mimetype" not in names:
                    raise ValidationError("The file is not a valid OpenDocument container.")
                if any(name.lower().endswith("vbaproject.bin") for name in names):
                    raise ValidationError("Macro-enabled Office documents are not accepted.")
                if len(names)>5000 or sum(item.file_size for item in archive.infolist())>50*1024*1024:
                    raise ValidationError("The document archive expands beyond permitted limits.")
        except (zipfile.BadZipFile,OSError):
            raise ValidationError("The uploaded document container is corrupt or invalid.")
        finally:
            upload.seek(0)
    if ext in TEXT_EXTENSIONS:
        try: upload.read(8192).decode("utf-8-sig")
        except UnicodeDecodeError: raise ValidationError("CSV, TXT and Markdown files must be UTF-8 encoded.")
        finally: upload.seek(0)
    return upload
def validate_avatar(upload):
    if upload.size>settings.MAX_AVATAR_UPLOAD_BYTES: raise ValidationError("Profile images must be 2 MB or smaller.")
    try:
        image=Image.open(upload); image.verify(); image=Image.open(upload)
        if image.width>settings.MAX_AVATAR_DIMENSION or image.height>settings.MAX_AVATAR_DIMENSION: raise ValidationError("Image dimensions must not exceed 2500 × 2500 pixels.")
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError): raise ValidationError("Upload a valid image file.")
    finally: upload.seek(0)
def sha256_upload(upload):
    digest=hashlib.sha256()
    for chunk in upload.chunks(): digest.update(chunk)
    upload.seek(0); return digest.hexdigest()
def clamav_scan(upload):
    if not settings.CLAMAV_ENABLED: return "not_configured","ClamAV is not configured; manual administrator review required."
    try:
        upload.seek(0); sock=socket.create_connection((settings.CLAMAV_HOST,settings.CLAMAV_PORT),timeout=settings.CLAMAV_TIMEOUT)
        with sock:
            sock.sendall(b"zINSTREAM\0")
            for chunk in upload.chunks(): sock.sendall(struct.pack("!I",len(chunk))+chunk)
            sock.sendall(struct.pack("!I",0)); parts=[]
            while True:
                part=sock.recv(4096)
                if not part: break
                parts.append(part)
                if b"\0" in part: break
        result=b"".join(parts).decode("utf-8","replace")
        if "FOUND" in result: return "infected",result[:450]
        if "OK" in result: return "clean",result[:450]
        return "error",result[:450]
    except Exception as exc: return "error",f"Scanner unavailable: {type(exc).__name__}"
    finally: upload.seek(0)
