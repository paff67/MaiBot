"""HEVC/WXGF 中图尺寸与 hdlength/length/hevc_mid_size 判定回归测试。"""

from __future__ import annotations

from src.phone_edge.media_references import message_media_refs


def _image_row(
    *,
    msg_id: str,
    talker: str,
    length: str,
    hdlength: str,
    hevc_mid_size: str,
    total_len: str,
    orig_md5: str = "a" * 32,
) -> dict:
    return {
        "msgId": msg_id,
        "msgSvrId": "9001" + msg_id,
        "type": "3",
        "talker": talker,
        "content": (
            f'<msg><img length="{length}" hdlength="{hdlength}" '
            f'hevc_mid_size="{hevc_mid_size}" md5="{orig_md5}" /></msg>'
        ),
        "imgPath": "THUMBNAIL_DIRPATH://th_abc",
        "_media_info": [
            {
                "table": "ImgInfo2",
                "msglocalid": msg_id,
                "msgSvrId": "9001" + msg_id,
                "bigImgPath": "big_downloaded.jpg",
                "midImgPath": None,
                "hevcPath": None,
                "offset": total_len,
                "totalLen": total_len,
                "origImgMD5": orig_md5,
                "msgTalker": talker,
            }
        ],
    }


def test_hevc_mid_size_distinct_from_length_is_kept_as_original() -> None:
    """复现线上 msg=143188 / msg=143241：totalLen==hevc_mid_size 且与 length/hdlength 不同时，不得降级为 medium。"""
    row_143188 = _image_row(
        msg_id="143188",
        talker="wxid_tester",
        length="164912",
        hdlength="693839",
        hevc_mid_size="46859",
        total_len="46859",
    )
    refs_143188 = message_media_refs(row_143188)
    big_refs = [r for r in refs_143188 if r.get("evidence") == "ImgInfo2_msglocalid"]
    assert len(big_refs) == 1
    assert big_refs[0]["variant"] == "original"
    assert big_refs[0]["expected_size"] == 46859
    assert big_refs[0]["expected_md5"] is None
    assert big_refs[0]["download_complete"] is True

    row_143241 = _image_row(
        msg_id="143241",
        talker="wxid_tester",
        length="106300",
        hdlength="167144",
        hevc_mid_size="27994",
        total_len="27994",
    )
    refs_143241 = message_media_refs(row_143241)
    big_241 = [r for r in refs_143241 if r.get("evidence") == "ImgInfo2_msglocalid"][0]
    assert big_241["variant"] == "original"
    assert big_241["expected_md5"] is None


def test_hdlength_original_preserves_orig_img_md5_verification() -> None:
    """当落盘尺寸精确等于 hdlength 时，保持 original 并强制校验 origImgMD5。"""
    orig_md5 = "0123456789abcdef0123456789abcdef"
    row = _image_row(
        msg_id="143500",
        talker="wxid_tester",
        length="164912",
        hdlength="693839",
        hevc_mid_size="46859",
        total_len="693839",
        orig_md5=orig_md5,
    )
    refs = message_media_refs(row)
    big_ref = [r for r in refs if r.get("evidence") == "ImgInfo2_msglocalid"][0]
    assert big_ref["variant"] == "original"
    assert big_ref["expected_md5"] == orig_md5
    assert big_ref["expected_size"] == 693839


def test_smaller_unmatched_size_still_downgrades_to_medium() -> None:
    """若 totalLen 既不等于 hdlength，也不等于 length 或 hevc_mid_size，则仍保守判定为 medium。"""
    row = _image_row(
        msg_id="143501",
        talker="wxid_tester",
        length="164912",
        hdlength="693839",
        hevc_mid_size="46859",
        total_len="8192",
    )
    refs = message_media_refs(row)
    big_ref = [r for r in refs if r.get("evidence") == "ImgInfo2_msglocalid"][0]
    assert big_ref["variant"] == "medium"
    assert big_ref["expected_md5"] is None
