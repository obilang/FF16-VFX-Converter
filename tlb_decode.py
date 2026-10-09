#!/usr/bin/env python3
"""Decoder for Final Fantasy XVI .tlb (character timeline) files (magic: FCTL)."""

import struct
import json
import sys
import os

MAGIC = b"FCTL"

ELEMENT_TYPE_NAMES = {
    1: "kTimelineElem_1", 2: "kTimelineElem_2", 3: "kTimelineElem_3",
    4: "kTimelineElem_4", 5: "kTimelineElem_5", 6: "kTimelineElem_6",
    7: "kTimelineElem_7", 8: "CameraAnimationRange", 9: "kTimelineElem_9",
    10: "BattleCondition", 11: "kTimelineElem_11", 12: "BulletTimeRange",
    13: "kTimelineElem_13", 14: "kTimelineElem_14", 15: "kTimelineElem_15",
    16: "kTimelineElem_16", 17: "ControlPermission", 19: "kTimelineElem_19",
    20: "kTimelineElem_20", 21: "kTimelineElem_21", 22: "kTimelineElem_22",
    23: "kTimelineElem_23", 24: "kTimelineElem_24", 25: "kTimelineElem_25",
    26: "kTimelineElem_26", 27: "AttackMovement", 28: "kTimelineElem_28",
    29: "kTimelineElem_29", 30: "kTimelineElem_30", 31: "PlaySoundTrigger",
    33: "AttachWeaponTemporaryRange", 34: "kTimelineElem_34", 35: "kTimelineElem_35",
    36: "kTimelineElem_36", 37: "LinkedElementRange", 38: "kTimelineElem_38",
    40: "kTimelineElem_40", 42: "kTimelineElem_42", 44: "kTimelineElem_44",
    45: "ModelSE", 46: "kTimelineElem_46", 47: "BattleMessageRange",
    48: "kTimelineElem_48", 49: "kTimelineElem_49", 50: "kTimelineElem_50",
    51: "EnableDestructorCollision", 52: "kTimelineElem_52", 53: "kTimelineElem_53",
    55: "kTimelineElem_55", 56: "kTimelineElem_56", 57: "PadVibration",
    58: "kTimelineElem_58", 59: "kTimelineElem_59", 60: "kTimelineElem_60",
    63: "kTimelineElem_63", 64: "kTimelineElem_64", 67: "kTimelineElem_67",
    68: "kTimelineElem_68", 69: "kTimelineElem_69", 70: "kTimelineElem_70",
    71: "kTimelineElem_71", 72: "kTimelineElem_72", 73: "kTimelineElem_73",
    74: "ControlRejectionRange", 76: "kTimelineElem_76", 78: "kTimelineElem_78",
    79: "kTimelineElem_79", 80: "kTimelineElem_80", 81: "kTimelineElem_81",
    82: "kTimelineElem_82", 83: "kTimelineElem_83", 84: "kTimelineElem_84",
    85: "kTimelineElem_85", 86: "kTimelineElem_86", 87: "kTimelineElem_87",
    88: "kTimelineElem_88",
    1001: "PlayAnimationRange", 1002: "kTimelineElem_1002", 1003: "kTimelineElem_1003",
    1004: "kTimelineElem_1004", 1005: "kTimelineElem_1005", 1006: "kTimelineElem_1006",
    1007: "FreezeAirAscendRateRange", 1008: "kTimelineElem_1008",
    1009: "EnableComboRange", 1010: "TurnToTarget", 1011: "SummonMagicShotCreate",
    1012: "MagicCreate", 1013: "kTimelineElem_1013", 1014: "EnableBattleFlagRange",
    1015: "kTimelineElem_1015", 1016: "PrecedeInputUnk", 1017: "kTimelineElem_1017",
    1018: "kTimelineElem_1018", 1019: "kTimelineElem_1019", 1020: "kTimelineElem_1020",
    1021: "kTimelineElem_1021", 1022: "kTimelineElem_1022", 1023: "kTimelineElem_1023",
    1024: "kTimelineElem_1024", 1025: "kTimelineElem_1025", 1026: "kTimelineElem_1026",
    1027: "kTimelineElem_1027", 1028: "kTimelineElem_1028", 1029: "kTimelineElem_1029",
    1030: "kTimelineElem_1030", 1031: "kTimelineElem_1031", 1032: "kTimelineElem_1032",
    1034: "kTimelineElem_1034", 1035: "PlayAnimationRange(1035)", 1037: "kTimelineElem_1037",
    1038: "kTimelineElem_1038", 1039: "kTimelineElem_1039", 1040: "kTimelineElem_1040",
    1041: "kTimelineElem_1041", 1042: "kTimelineElem_1042", 1043: "kTimelineElem_1043",
    1044: "kTimelineElem_1044", 1045: "kTimelineElem_1045", 1046: "kTimelineElem_1046",
    1047: "SummonPartsVisibleRange", 1048: "kTimelineElem_1048", 1049: "kTimelineElem_1049",
    1050: "kTimelineElem_1050", 1051: "kTimelineElem_1051", 1052: "kTimelineElem_1052",
    1053: "BattleVoiceTrigger", 1054: "kTimelineElem_1054", 1055: "kTimelineElem_1055",
    1056: "kTimelineElem_1056", 1057: "kTimelineElem_1057", 1058: "DisableReceiver",
    1059: "kTimelineElem_1059", 1060: "kTimelineElem_1060", 1061: "kTimelineElem_1061",
    1062: "kTimelineElem_1062", 1063: "kTimelineElem_1063", 1064: "kTimelineElem_1064",
    1065: "kTimelineElem_1065", 1066: "kTimelineElem_1066", 1067: "kTimelineElem_1067",
    1068: "kTimelineElem_1068", 1070: "kTimelineElem_1070", 1071: "kTimelineElem_1071",
    1072: "kTimelineElem_1072", 1073: "kTimelineElem_1073", 1074: "kTimelineElem_1074",
    1075: "kTimelineElem_1075", 1077: "kTimelineElem_1077", 1078: "kTimelineElem_1078",
    1079: "kTimelineElem_1079", 1080: "kTimelineElem_1080", 1081: "kTimelineElem_1081",
    1082: "kTimelineElem_1082", 1083: "kTimelineElem_1083", 1084: "EnableMagicBurstRange",
    1085: "kTimelineElem_1085", 1086: "AirborneFallRange", 1087: "kTimelineElem_1087",
    1088: "ApplyCharacterForceTrigger", 1089: "kTimelineElem_1089", 1091: "kTimelineElem_1091",
    1092: "kTimelineElem_1092", 1093: "kTimelineElem_1093", 1094: "kTimelineElem_1094",
    1095: "kTimelineElem_1095", 1096: "kTimelineElem_1096", 1097: "DisableCharaUnk",
    1098: "kTimelineElem_1098", 1099: "kTimelineElem_1099", 1100: "kTimelineElem_1100",
    1101: "kTimelineElem_1101", 1102: "kTimelineElem_1102", 1103: "kTimelineElem_1103",
    1104: "kTimelineElem_1104", 1105: "kTimelineElem_1105", 1106: "kTimelineElem_1106",
    1107: "kTimelineElem_1107", 1108: "kTimelineElem_1108", 1109: "kTimelineElem_1109",
    1110: "kTimelineElem_1110", 1111: "kTimelineElem_1111", 1113: "kTimelineElem_1113",
    1114: "kTimelineElem_1114", 1115: "kTimelineElem_1115", 1116: "kTimelineElem_1116",
    1117: "ActionEventTrigger", 1118: "kTimelineElem_1118", 1119: "kTimelineElem_1119",
    1120: "kTimelineElem_1120", 1121: "kTimelineElem_1121", 1122: "kTimelineElem_1122",
    1123: "kTimelineElem_1123", 1124: "kTimelineElem_1124", 1125: "kTimelineElem_1125",
    1126: "kTimelineElem_1126", 1127: "kTimelineElem_1127", 1129: "kTimelineElem_1129",
    1130: "EnablePrecisionTorgalRange", 1131: "kTimelineElem_1131", 1132: "kTimelineElem_1132",
    1133: "kTimelineElem_1133", 1134: "kTimelineElem_1134", 1135: "kTimelineElem_1135",
    1136: "kTimelineElem_1136", 1137: "kTimelineElem_1137",
    3082: "kTimelineElem_3082", 3104: "kTimelineElem_3104",
    3109: "kTimelineElem_3109", 3135: "kTimelineElem_3135",
}


def u8(data, off): return struct.unpack_from("<B", data, off)[0]
def u16(data, off): return struct.unpack_from("<H", data, off)[0]
def i32(data, off): return struct.unpack_from("<i", data, off)[0]
def u32(data, off): return struct.unpack_from("<I", data, off)[0]
def f32(data, off): return struct.unpack_from("<f", data, off)[0]
def f64(data, off): return struct.unpack_from("<d", data, off)[0]

def read_cstr(data, off):
    end = data.index(b'\x00', off)
    return data[off:end].decode("utf-8", errors="replace")

def safe_cstr(data, base, rel_off):
    """Read a C-string at base+rel_off, or '' if out of range/zero offset."""
    if rel_off == 0 or base + rel_off >= len(data):
        return ""
    return read_cstr(data, base + rel_off)


def decode_asset_ref(data, asset_ref_start):
    """AssetRef: int AssetType, int AssetPathOffset (relative to struct start)."""
    asset_type = i32(data, asset_ref_start)
    path_offset = i32(data, asset_ref_start + 4)
    path = safe_cstr(data, asset_ref_start, path_offset) if path_offset else ""
    return {"asset_type": asset_type, "path": path}


VFX_EMIT_PARAMS_SIZE = 0x58


def decode_vfx_emit_params(data, base):
    """VFXEmitParams (0x58 bytes): spawn transform for one VFX instance.

    EidId2 wins if non-zero, else EidId1 is used (per .bt comment) as the
    attach-point/bone id (resolved via the character .mdl's EidDataDef
    table); XOffset/YOffset/ZOffset are a local offset from that socket,
    Revolution orbits around the character, Rotation/VFXScale are applied
    on top.
    """
    active = i32(data, base + 0x00)
    unk_id_slot = i32(data, base + 0x04)
    eid_id1 = i32(data, base + 0x08)
    eid_id2 = i32(data, base + 0x0C)
    x = f64(data, base + 0x10)
    y = f64(data, base + 0x18)
    z = f64(data, base + 0x20)
    revolution = f64(data, base + 0x28)
    rotation = f32(data, base + 0x30)
    scale = f32(data, base + 0x34)
    return {
        "active": bool(active),
        "unk_id_slot": unk_id_slot,
        "eid_id1": eid_id1,
        "eid_id2": eid_id2,
        "attach_eid": eid_id2 if eid_id2 else eid_id1,
        "offset": {"x": x, "y": y, "z": z},
        "revolution": revolution,
        "rotation": rotation,
        "scale": scale,
    }


def decode_vfx_emit_params_array(data, base, count):
    out = []
    for i in range(count):
        entry_base = base + i * VFX_EMIT_PARAMS_SIZE
        if entry_base + VFX_EMIT_PARAMS_SIZE > len(data):
            break
        out.append(decode_vfx_emit_params(data, entry_base))
    return out


VFX_EXTERNAL_PARAM_SIZE = 0x1C


def decode_vfx_external_param(data, base):
    """One "vfxexternallist" entry (0x1C bytes) — a per-spawn override for a
    named parameter inside the referenced .vfxb graph (id = hash/index of a
    shader constant or curve in that file), analogous to a Niagara User
    Parameter override. Verified across ~2000 samples: field_0x10 is always
    the sentinel -1 (not a pointer/offset).
    """
    param_id = i32(data, base + 0x00)
    kind = i32(data, base + 0x04)      # seen: 0, 2, 5, 6 - selects value type/target
    unk_0x08 = i32(data, base + 0x08)  # seen: 1, 4
    value = f32(data, base + 0x0C)     # override value (seen 0.0 .. 7.0)
    return {
        "param_id": param_id,
        "kind": kind,
        "unk_0x08": unk_0x08,
        "value": value,
    }


def decode_vfx_external_params_array(data, base, count):
    out = []
    for i in range(count):
        entry_base = base + i * VFX_EXTERNAL_PARAM_SIZE
        if entry_base + VFX_EXTERNAL_PARAM_SIZE > len(data):
            break
        out.append(decode_vfx_external_param(data, entry_base))
    return out


def decode_element_data(data, union_start, union_type):
    """
    Decode the element-specific data from a TimelineElementDataUnion.
    union_start = absolute offset to the union (where UnionType lives).
    element_data_start = union_start + 0x10 (after UnionType + 3 int fields).
    """
    ed = union_start + 0x10  # ElementData start

    t = union_type
    result = {}

    try:
        if t == 5:
            result["bool_0x00"] = u8(data, ed)
            result["bool_0x01"] = u8(data, ed+1)
            result["bool_0x02"] = u8(data, ed+2)
            result["chara_collision_shape_id"] = i32(data, ed+4)

        elif t == 8:  # CameraAnimationRange
            result["num_frames"] = i32(data, ed)
            result["field_0x04"] = i32(data, ed+4)
            result["curves"] = "(skipped - complex Curve structs)"

        elif t == 9:
            result["attack_param_id"] = i32(data, ed)
            result["unk_type"] = i32(data, ed+4)
            result["field_0x08"] = i32(data, ed+8)
            result["eid_id"] = i32(data, ed+12)

        elif t == 10:  # BattleCondition
            for i in range(9):
                result[f"bool_{i:02x}"] = u8(data, ed+i)
            result["field_0x0c"] = f32(data, ed+12)
            result["field_0x10"] = i32(data, ed+16)
            result["field_0x14"] = f32(data, ed+20)

        elif t == 11:
            result["motion_layer_id"] = i32(data, ed)
            result["seconds_maybe"] = i32(data, ed+4)
            result["field_0x08"] = i32(data, ed+8)
            result["field_0x0c"] = i32(data, ed+12)

        elif t == 12:  # BulletTimeRange
            result["field_0x00"] = f32(data, ed)
            result["field_0x04"] = f32(data, ed+4)
            for i in range(8):
                result[f"bool_{i:02x}"] = u8(data, ed+8+i)
            result["vatb_entry_index_maybe"] = i32(data, ed+16)

        elif t == 17:  # ControlPermission
            names0 = ["0x02","0x400","0x80","0x100","0x04","0x08","0x200","0x10","0x20","0x800","0x1000","0x2000","0x4000","0x8000"]
            names1 = ["0x02","0x400","unk","0x80","0x100","0x04","0x08","0x200","0x10","0x20","0x800","0x1000","0x2000","0x4000","0x8000"]
            result["bool0"] = {names0[i]: u8(data, ed+i) for i in range(min(len(names0), 14))}
            result["bool1"] = {names1[i]: u8(data, ed+16+i) for i in range(min(len(names1), 15))}

        elif t in (23, 24):
            result["pad"] = data[ed:ed+0x20].hex()

        elif t == 27:  # AttackMovement
            result["bool_0x00"] = u8(data, ed)

        elif t == 30:
            result["anim"] = decode_asset_ref(data, ed)
            result["field_0x08"] = i32(data, ed+8)
            result["field_0x0c"] = u8(data, ed+12)
            result["field_0x10"] = i32(data, ed+16)
            result["field_0x14"] = i32(data, ed+20)
            result["field_0x18"] = f64(data, ed+24)
            result["field_0x20"] = f64(data, ed+32)
            result["field_0x28"] = f64(data, ed+40)
            result["field_0x30"] = i32(data, ed+48)
            result["field_0x34"] = i32(data, ed+52)
            result["field_0x38"] = i32(data, ed+56)
            result["field_0x3c"] = f32(data, ed+60)

        elif t == 31:  # PlaySoundTrigger
            result["sound"] = decode_asset_ref(data, ed)
            result["field_0x08"] = i32(data, ed+8)
            result["field_0x0c"] = u8(data, ed+12)
            result["field_0x10"] = i32(data, ed+16)
            result["sab_entry_index_maybe"] = i32(data, ed+20)
            result["field_0x18"] = f64(data, ed+24)
            result["field_0x20"] = f64(data, ed+32)
            result["field_0x28"] = f64(data, ed+40)
            result["field_0x30"] = i32(data, ed+48)
            result["play_vfx_trigger_set_id"] = i32(data, ed+52)
            result["field_0x38"] = i32(data, ed+56)
            result["field_0x3c"] = f32(data, ed+60)
            result["bool_0x40"] = u8(data, ed+64)

        elif t == 33:  # AttachWeaponTemporaryRange
            result["field_0x00"] = i32(data, ed)
            result["field_0x04"] = i32(data, ed+4)

        elif t == 37:  # LinkedElementRange
            # field_0x2c: union_type of another element in the same timeline
            # this element is linked to (100% match across a 1012-sample
            # corpus scan); 0 means "no link" (then field_0x30/0x38 flip to
            # a fixed 60/1 sentinel and field_0x3c is always 48).
            linked_type = i32(data, ed+0x2c)
            result["packed_0x00"] = u32(data, ed)
            result["field_0x04"] = f32(data, ed+4)
            result["field_0x08"] = f32(data, ed+8)
            result["field_0x0c"] = f32(data, ed+12)
            result["field_0x14"] = f32(data, ed+20)
            result["field_0x18"] = i32(data, ed+24)
            result["field_0x1c"] = i32(data, ed+28)
            result["linked_element_type"] = linked_type
            result["linked_element_type_name"] = (
                ELEMENT_TYPE_NAMES.get(linked_type, f"unknown_{linked_type}")
                if linked_type else None
            )
            result["field_0x3c"] = i32(data, ed+60)

        elif t == 45:  # ModelSE
            result["se_index"] = i32(data, ed)
            result["bool_0x04"] = u8(data, ed+4)
            sound_path_offset = i32(data, ed+8)
            result["path"] = safe_cstr(data, ed, sound_path_offset) if sound_path_offset else ""
            result["field_0x0c"] = i32(data, ed+12)
            result["field_0x10"] = f64(data, ed+16)
            result["field_0x18"] = f64(data, ed+24)
            result["field_0x20"] = f64(data, ed+32)

        elif t == 47:  # BattleMessageRange
            result["battle_message_id"] = i32(data, ed)

        elif t == 49:
            result["mseq_input_id"] = i32(data, ed)

        elif t in (56, 57):  # PadVibration
            result["camera_fcurve_id"] = i32(data, ed)
            unk_off1 = i32(data, ed+4)
            unk_off2 = i32(data, ed+12)
            unk_off3 = i32(data, ed+20)
            result["name1"] = safe_cstr(data, union_start, unk_off1) if unk_off1 else ""
            result["name2"] = safe_cstr(data, union_start, unk_off2) if unk_off2 else ""
            result["name3"] = safe_cstr(data, union_start, unk_off3) if unk_off3 else ""
            result["field_0x08"] = i32(data, ed+8)
            result["field_0x10"] = i32(data, ed+16)
            result["field_0x18"] = i32(data, ed+24)
            result["field_0x20"] = f32(data, ed+32)
            result["field_0x24"] = i32(data, ed+36)
            result["field_0x28"] = f64(data, ed+40)
            result["field_0x30"] = f32(data, ed+48)
            result["field_0x34"] = i32(data, ed+52)

        elif t == 51:  # EnableDestructorCollision
            anim_path_offset = i32(data, ed)
            result["path"] = safe_cstr(data, union_start, anim_path_offset) if anim_path_offset else ""

        elif t in (60, 73):
            off1 = i32(data, ed+4)
            off2 = i32(data, ed+12)
            off3 = i32(data, ed+20)
            result["field_0x00"] = i32(data, ed)
            result["name1"] = safe_cstr(data, union_start, off1) if off1 else ""
            result["field_0x08"] = i32(data, ed+8)
            result["name2"] = safe_cstr(data, union_start, off2) if off2 else ""
            result["field_0x10"] = i32(data, ed+16)
            result["name3"] = safe_cstr(data, union_start, off3) if off3 else ""

        elif t == 74:  # ControlRejectionRange
            result["field_0x00"] = i32(data, ed)
            result["flags"] = data[ed+4:ed+0x13].hex()

        elif t in (1001, 1035):  # PlayAnimationRange
            result["anim"] = decode_asset_ref(data, ed)
            result["bool_0x08"] = u8(data, ed+8)
            result["bool_0x09"] = u8(data, ed+9)
            result["bool_0x0a"] = u8(data, ed+10)
            result["float_0x0c"] = f32(data, ed+12)
            result["bool_0x10"] = u8(data, ed+16)
            result["bool_0x11"] = u8(data, ed+17)
            result["bool_0x12"] = u8(data, ed+18)

        elif t == 1002:
            result["attack_param_id"] = i32(data, ed)
            name_off = i32(data, ed+4)
            result["bool_0x08"] = u8(data, ed+8)
            result["field_0x0c"] = i32(data, ed+12)
            name2_off = i32(data, ed+16)
            result["bool_0x18"] = u8(data, ed+24)
            result["name"] = safe_cstr(data, union_start, name_off) if name_off else ""
            result["name2"] = safe_cstr(data, union_start, name2_off) if name2_off else ""

        elif t == 1004:
            result["field_0x00"] = i32(data, ed)
            result["field_0x04"] = f32(data, ed+4)
            result["field_0x08"] = f32(data, ed+8)

        elif t == 1007:  # FreezeAirAscendRateRange
            result["flag"] = u8(data, ed)

        elif t == 1009:  # EnableComboRange
            result["pad"] = data[ed:ed+0x20].hex()

        elif t == 1010:  # TurnToTarget
            result["type"] = i32(data, ed)
            result["target_type"] = i32(data, ed+4)
            result["layout_instance_id"] = i32(data, ed+8)
            result["field_0x0c"] = f32(data, ed+12)
            result["field_0x10"] = i32(data, ed+16)
            result["field_0x14"] = f32(data, ed+20)
            result["field_0x18"] = f32(data, ed+24)
            result["unk_type"] = i32(data, ed+28)
            result["field_0x20"] = f32(data, ed+32)

        elif t == 1011:  # SummonMagicShotCreate
            result["summon_magic_source_type"] = i32(data, ed)
            result["custom_bool1"] = u8(data, ed+4)
            result["custom_bool2"] = u8(data, ed+5)
            result["custom_magic_id_skill1"] = i32(data, ed+8)
            result["custom_magic_id_skill2"] = i32(data, ed+12)

        elif t == 1012:  # MagicCreate
            result["unused"] = i32(data, ed)
            result["magic_id"] = i32(data, ed+4)
            result["unk_bool_use_other_position"] = u8(data, ed+8)
            result["has_target_maybe"] = u8(data, ed+9)

        elif t == 1014:  # EnableBattleFlagRange
            result["flag"] = i32(data, ed)

        elif t == 1016:  # PrecedeInputUnk
            result["flags"] = data[ed:ed+6].hex()
            result["unk1"] = i32(data, ed+8)
            result["unk2"] = i32(data, ed+12)
            result["unk3"] = i32(data, ed+16)

        elif t == 1023:
            vfx_params_off = i32(data, ed)
            vfx_param_count = i32(data, ed+4)
            result["vfx_params_count"] = vfx_param_count
            result["field_0x08"] = i32(data, ed+8)
            result["vfx"] = decode_asset_ref(data, ed+16)
            result["se"] = decode_asset_ref(data, ed+24)
            result["play_vfx_trigger_set_id_maybe"] = i32(data, ed+52)
            if vfx_param_count > 0:
                result["vfx_emit_params"] = decode_vfx_emit_params_array(
                    data, ed + vfx_params_off, vfx_param_count)

        elif t == 1030:
            vfx_params_off = i32(data, ed)
            vfx_param_count = i32(data, ed+4)
            result["vfx_params_count"] = vfx_param_count
            external_off = i32(data, ed+8)
            external_count = i32(data, ed+12)
            result["vfx"] = decode_asset_ref(data, ed+16)
            result["se"] = decode_asset_ref(data, ed+24)
            unk_name3_off = i32(data, ed+52)
            result["unk_name3"] = safe_cstr(data, union_start, unk_name3_off) if unk_name3_off else ""
            if vfx_param_count > 0:
                result["vfx_emit_params"] = decode_vfx_emit_params_array(
                    data, ed + vfx_params_off, vfx_param_count)
            if external_count > 0:
                result["vfx_external_params"] = decode_vfx_external_params_array(
                    data, ed + external_off, external_count)

        elif t == 1047:  # SummonPartsVisibleRange
            result["summon_parts_pattern_id"] = i32(data, ed)
            result["field_0x04"] = f32(data, ed+4)
            result["field_0x08"] = f32(data, ed+8)
            result["field_0x0c"] = u8(data, ed+12)
            result["field_0x0d"] = u8(data, ed+13)
            result["field_0x0e"] = u8(data, ed+14)
            result["field_0x0f"] = u8(data, ed+15)
            result["field_0x10"] = f32(data, ed+16)

        elif t == 1049:
            vfx_params_off = i32(data, ed)
            vfx_param_count = i32(data, ed+4)
            result["vfx_params_count"] = vfx_param_count
            external_off = i32(data, ed+8)
            external_count = i32(data, ed+12)
            result["vfx"] = decode_asset_ref(data, ed+16)
            result["se"] = decode_asset_ref(data, ed+24)
            if vfx_param_count > 0:
                result["vfx_emit_params"] = decode_vfx_emit_params_array(
                    data, ed + vfx_params_off, vfx_param_count)
            if external_count > 0:
                result["vfx_external_params"] = decode_vfx_external_params_array(
                    data, ed + external_off, external_count)

        elif t == 1053:  # BattleVoiceTrigger
            result["field_0x00"] = i32(data, ed)
            result["battle_voice_category_id"] = i32(data, ed+4)

        elif t == 1056:
            result["frame_count_maybe"] = i32(data, ed)
            result["frame_count2_maybe"] = i32(data, ed+4)

        elif t == 1058:  # DisableReceiver
            name_off = i32(data, ed)
            result["path"] = safe_cstr(data, union_start, name_off) if name_off else ""

        elif t == 1059:
            name_off = i32(data, ed)
            result["path"] = safe_cstr(data, union_start, name_off) if name_off else ""
            result["field_0x04"] = f32(data, ed+4)

        elif t == 1064:
            result["unk_id"] = i32(data, ed)
            name_off = i32(data, ed+4)
            result["name"] = safe_cstr(data, union_start, name_off) if name_off else ""

        elif t == 1066:
            result["field_0x00"] = i32(data, ed)

        elif t == 1075:
            result["field_0x00"] = i32(data, ed)

        elif t == 1084:  # EnableMagicBurstRange
            result["unk_type"] = i32(data, ed)

        elif t == 1086:  # AirborneFallRange
            result["height_fall"] = f32(data, ed)
            result["only_if_target_hit"] = u8(data, ed+4)
            result["unk"] = u8(data, ed+5)

        elif t == 1088:  # ApplyCharacterForceTrigger
            result["horizontal_type"] = i32(data, ed)
            result["horizontal_force"] = f32(data, ed+4)
            result["horizontal_rate"] = f32(data, ed+8)
            result["unused"] = i32(data, ed+12)
            result["vertical_type"] = i32(data, ed+16)
            result["vertical_force"] = f32(data, ed+20)
            result["vertical_rate"] = f32(data, ed+24)
            result["field_0x1c"] = u8(data, ed+28)

        elif t == 1097:  # DisableCharaUnk
            name_off = i32(data, ed)
            result["bool_0x04"] = u8(data, ed+4)
            result["name"] = safe_cstr(data, union_start, name_off) if name_off else ""

        elif t == 1099:
            result["field_0x00"] = i32(data, ed)
            result["bool_0x04"] = u8(data, ed+4)
            result["field_0x08"] = f32(data, ed+8)
            result["unk_frames1"] = i32(data, ed+12)
            result["unk_frames2"] = i32(data, ed+16)
            result["field_0x14"] = f32(data, ed+20)
            result["field_0x18"] = f32(data, ed+24)
            result["field_0x1c"] = f32(data, ed+28)

        elif t in (1102, 1103, 1107):
            off = i32(data, ed)
            result["path"] = safe_cstr(data, union_start, off) if off else ""

        elif t == 1115:
            result["frames_maybe"] = i32(data, ed)
            result["field_0x04"] = i32(data, ed+4)
            result["field_0x08"] = f32(data, ed+8)

        elif t == 1117:  # ActionEventTrigger
            result["unk_id"] = i32(data, ed)

        elif t == 1130:  # EnablePrecisionTorgalRange
            result["pad"] = data[ed:ed+0x20].hex()

        else:
            # Unknown type: dump raw bytes (up to 64)
            end = min(ed + 64, len(data))
            result["raw"] = data[ed:end].hex()

    except Exception as e:
        result["decode_error"] = str(e)
        try:
            end = min(ed + 64, len(data))
            result["raw"] = data[ed:end].hex()
        except:
            pass

    return result


def decode_asset_group(data, timeline_base, group_start):
    """Decode one AssetGroup (0x0C bytes header at group_start)."""
    index = i32(data, group_start)
    asset_array_list_offset = i32(data, group_start + 4)
    num_assets = i32(data, group_start + 8)

    assets = []
    if num_assets > 0:
        offsets_base = group_start + asset_array_list_offset
        for j in range(num_assets):
            entry_off = i32(data, offsets_base + j * 4)
            entry_start = offsets_base + entry_off
            fname_off = i32(data, entry_start + 0x14)
            path_off = i32(data, entry_start + 0x18)
            filename = safe_cstr(data, entry_start, fname_off) if fname_off else ""
            path = safe_cstr(data, entry_start, path_off) if path_off else ""
            assets.append({
                "field_0x00": i32(data, entry_start),
                "field_0x04": i32(data, entry_start + 4),
                "field_0x08": i32(data, entry_start + 8),
                "field_0x0c": i32(data, entry_start + 12),
                "field_0x10": i32(data, entry_start + 16),
                "filename": filename,
                "path": path,
            })

    return {"index": index, "assets": assets}


def decode_tlb(data):
    # Header
    magic = data[0:4]
    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic!r}, expected {MAGIC!r}")

    version = i32(data, 4)
    # Padding[4] at 0x08
    timeline_offset = i32(data, 0x18)

    tb = timeline_offset  # Timeline base

    editor_id = i32(data, tb + 0x00)
    elem_offset = i32(data, tb + 0x04)  # relative to tb
    elem_count = i32(data, tb + 0x08)
    asset_groups_offset = i32(data, tb + 0x0C)  # relative to tb
    asset_group_count = i32(data, tb + 0x10)
    targets_array_offset = i32(data, tb + 0x14)  # relative to tb
    target_count = i32(data, tb + 0x18)
    total_frames = i32(data, tb + 0x1C)
    bool_0x20 = u8(data, tb + 0x20)

    timeline_elements = []
    for i in range(elem_count):
        elem_start = tb + elem_offset + i * 0x20
        field_0x00 = i32(data, elem_start + 0x00)
        unk_name_offset = i32(data, elem_start + 0x04)
        layer_id = i32(data, elem_start + 0x08)
        frame_start = i32(data, elem_start + 0x0C)
        num_frames = i32(data, elem_start + 0x10)
        field_0x14 = i32(data, elem_start + 0x14)
        b18 = u8(data, elem_start + 0x18)
        b19 = u8(data, elem_start + 0x19)
        b1a = u8(data, elem_start + 0x1A)
        b1b = u8(data, elem_start + 0x1B)
        data_offset = i32(data, elem_start + 0x1C)

        name = safe_cstr(data, elem_start, unk_name_offset) if unk_name_offset else ""

        union_start = elem_start + data_offset
        union_type = i32(data, union_start + 0x00)
        union_f04 = i32(data, union_start + 0x04)
        union_f08 = i32(data, union_start + 0x08)
        union_f0c = i32(data, union_start + 0x0C)

        type_name = ELEMENT_TYPE_NAMES.get(union_type, f"unknown_{union_type}")
        elem_data = decode_element_data(data, union_start, union_type)

        timeline_elements.append({
            "index": i,
            "field_0x00": field_0x00,
            "name": name,
            "layer_id": layer_id,
            "frame_start": frame_start,
            "num_frames": num_frames,
            "field_0x14": field_0x14,
            "field_0x18": [b18, b19, b1a, b1b],
            "union_type": union_type,
            "type_name": type_name,
            "union_field_0x04": union_f04,
            "union_field_0x08": union_f08,
            "union_field_0x0c": union_f0c,
            "data": elem_data,
        })

    asset_groups = []
    for i in range(asset_group_count):
        group_start = tb + asset_groups_offset + i * 0x0C
        asset_groups.append(decode_asset_group(data, tb, group_start))

    targets = []
    targets_base = tb + targets_array_offset
    if target_count > 0:
        for i in range(target_count):
            off = i32(data, targets_base + i * 4)
            t_start = targets_base + off
            t_type = i32(data, t_start)
            targets.append({
                "type": t_type,
                "pad": data[t_start+4:t_start+0x28].hex(),
            })

    return {
        "magic": magic.decode("ascii"),
        "version": version,
        "timeline_offset": hex(timeline_offset),
        "editor_id": editor_id,
        "total_frames": total_frames,
        "bool_0x20": bool_0x20,
        "timeline_element_count": elem_count,
        "asset_group_count": asset_group_count,
        "target_count": target_count,
        "timeline_elements": timeline_elements,
        "asset_groups": asset_groups,
        "targets": targets,
    }


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <file.tlb> [output.json]")
        sys.exit(1)

    in_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(in_path)[0] + ".json"

    with open(in_path, "rb") as f:
        data = f.read()

    result = decode_tlb(data)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"Decoded {len(result['timeline_elements'])} elements -> {out_path}")
    print(f"Total frames: {result['total_frames']}")
    for elem in result['timeline_elements']:
        print(f"  [{elem['frame_start']:4d}..{elem['frame_start']+elem['num_frames']:4d}] "
              f"{elem['type_name']:40s}  name={elem['name']!r}")


if __name__ == "__main__":
    main()
