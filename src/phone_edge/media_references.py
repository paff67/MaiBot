"""Parse media identity from a committed message, never from arrival-time proximity."""

from typing import Any, Dict, List
import re
import xml.etree.ElementTree as ET


def message_media_refs(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_type = int(row.get("type") or 0)
    kind = raw_type & 0xFFFF
    if kind not in {3, 34, 43, 47, 49}:
        return []
    content = row.get("content") or ""
    if len(content) > 4_000_000 or re.search(r"<!\s*(DOCTYPE|ENTITY)", content, re.I):
        raise ValueError("Unsafe or oversized media XML")
    start, end = content.find("<msg"), content.rfind("</msg>")
    root = None
    if start >= 0 and end >= start:
        try:
            root = ET.fromstring(content[start : end + 6])
        except ET.ParseError as exc:
            raise ValueError("Malformed media XML; retained for inspection") from exc
    path = row.get("imgPath") or ""
    refs: List[Dict[str, Any]] = []

    def add(media_kind, variant, names, md5=None, size=None):
        safe = sorted({n for n in names if n and re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", n) and n not in {".", ".."}})
        digest = md5.lower() if isinstance(md5, str) and re.fullmatch(r"[0-9a-fA-F]{32}", md5) else None
        expected_size = int(size) if size is not None and str(size).isdigit() and int(size) > 0 else None
        if safe:
            refs.append(
                dict(
                    slot=0,
                    kind=media_kind,
                    variant=variant,
                    names=safe,
                    expected_md5=digest,
                    expected_size=expected_size,
                    evidence="committed_message_reference",
                )
            )

    if kind == 3:
        image = root.find("img") if root is not None else None
        if path.startswith("THUMBNAIL_DIRPATH://"):
            add("image", "thumbnail", [path.split("://", 1)[1]])
        elif path:
            add("image", "referenced", [path])
        if image is not None:
            digest = image.get("md5")
            if digest and re.fullmatch(r"[0-9a-fA-F]{32}", digest):
                add("image", "original", [digest, digest + ".jpg", digest + ".png", digest + ".webp"], digest)
    elif kind == 34:
        if path and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", path):
            name = path if path.startswith("msg_") else "msg_" + path
            add("voice", "original", [name if name.endswith(".amr") else name + ".amr", path])
    elif kind == 43:
        if path and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", path):
            add("video", "original", [path if path.endswith(".mp4") else path + ".mp4"])
    elif kind == 47:
        emoji = root.find("emoji") if root is not None else None
        digest = emoji.get("md5") if emoji is not None else path
        if not digest or not re.fullmatch(r"[0-9a-fA-F]{32}", digest):
            digest = path if re.fullmatch(r"[0-9a-fA-F]{32}", path) else None
        if digest and re.fullmatch(r"[0-9a-fA-F]{32}", digest):
            add(
                "emoji",
                "original",
                [digest, digest + ".png", digest + ".gif", digest + ".wxgf", digest + "_cover"],
                digest,
            )
    elif kind == 49:
        app = root.find("appmsg") if root is not None else None
        if app is None:
            raise ValueError("Application message missing parsable appmsg XML")
        # Link/card/forwarded-history messages are not ordinary file attachments.
        if app.findtext("type") != "6":
            return []
        attachment = app.find("appattach")
        if attachment is None:
            raise ValueError("File message missing appattach")
        digest = attachment.findtext("md5") or app.findtext("md5")
        title = app.findtext("title") or ""
        size = attachment.findtext("totallen")
        if digest and re.fullmatch(r"[0-9a-fA-F]{32}", digest):
            # Shared download directories require checksum evidence, not filename alone.
            refs.append(
                dict(
                    slot=0,
                    kind="document",
                    variant="original",
                    names=[title] if title else [],
                    expected_md5=digest.lower(),
                    expected_size=int(size) if size and size.isdigit() else None,
                    evidence="message_attachment_checksum",
                )
            )
    for info in row.get("_media_info", []):
        owner = info.get("msgTalker") or info.get("MsgTalker") or info.get("msg_talker") or info.get("msgInfoTalker")
        local_id = info.get("msglocalid") or info.get("MsgLocalId") or info.get("msgInfoId")
        local_match = local_id not in (None, "", "0", 0) and str(local_id) == str(row.get("msgId"))
        server_match = (
            info.get("table") == "ImgInfo2"
            and local_id in (None, "", "0", 0)
            and str(info.get("msgSvrId") or "") not in ("", "0")
            and str(info["msgSvrId"]) == str(row.get("msgSvrId"))
            and owner == row.get("talker")
            and bool(owner)
        )
        if not (local_match or server_match) or (owner and owner != row.get("talker")):
            raise ValueError("Secondary media table ownership mismatch")
        if kind == 3 and info.get("table") == "ImgInfo2":
            for field, variant in [("bigImgPath", "original"), ("midImgPath", "medium"), ("hevcPath", "opaque")]:
                local = info.get(field)
                if local and "://" not in local:
                    total = int(info.get("totalLen") or 0)
                    original_size = int(image.get("hdlength") or 0) if image is not None else 0
                    mid_size = int(image.get("length") or image.get("hevc_mid_size") or 0) if image is not None else 0
                    if field == "bigImgPath" and original_size and total not in {original_size, mid_size}:
                        variant = "medium"
                    refs.append(
                        dict(
                            slot=0,
                            kind="image",
                            variant=variant,
                            names=[local.rsplit("/", 1)[-1]],
                            exact_path=local,
                            expected_md5=info["origImgMD5"].lower()
                            if variant == "original"
                            and (not original_size or total == original_size)
                            and re.fullmatch(r"[0-9a-fA-F]{32}", info.get("origImgMD5") or "")
                            else None,
                            expected_size=total if total > 0 and field == "bigImgPath" else None,
                            evidence="ImgInfo2_msglocalid" if local_match else "ImgInfo2_msgSvrId_talker",
                            download_complete=total > 0 and str(info.get("offset")) == str(total),
                        )
                    )
        elif kind == 49 and info.get("table") == "appattach" and info.get("fileFullPath"):
            local = info["fileFullPath"]
            refs.append(
                dict(
                    slot=0,
                    kind="document",
                    variant="original",
                    names=[local.rsplit("/", 1)[-1]],
                    exact_path=local,
                    expected_md5=digest.lower() if digest and re.fullmatch(r"[0-9a-fA-F]{32}", digest) else None,
                    expected_size=int(info["totalLen"]) if str(info.get("totalLen") or "").isdigit() else None,
                    evidence="appattach_msgInfoId",
                    download_complete=info.get("offset") == info.get("totalLen")
                    and info.get("totalLen") not in (None, "0"),
                )
            )
    if not refs:
        raise ValueError("Media message has no safe local reference; requires secondary-table lookup")
    return refs
