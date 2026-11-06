"""ctypes wrapper around libbcnhelper.so (bytecoin's own serialization + CryptoNight)."""
import ctypes
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


class Helper:
    def __init__(self, path=os.path.join(_HERE, "libbcnhelper.so")):
        self.lib = ctypes.CDLL(path)
        L = self.lib
        sz = ctypes.POINTER(ctypes.c_size_t)
        L.bcn_last_error.restype = ctypes.c_char_p
        L.bcn_prepare_template.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
                                           ctypes.c_char_p, sz, ctypes.c_char_p, sz, sz, sz]
        L.bcn_hashing_blob_from_json.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, sz]
        L.bcn_template_from_json.argtypes = [ctypes.c_char_p, ctypes.c_char_p, sz]
        L.bcn_hashing_blob.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_char_p, sz]
        L.bcn_cn_slow_hash.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p]
        L.bcn_cn_slow_hash.restype = None
        L.bcn_check_hash.argtypes = [ctypes.c_char_p, ctypes.c_uint64]

    def _err(self):
        return RuntimeError(self.lib.bcn_last_error().decode(errors="replace"))

    def prepare_template(self, tmpl, currency_id):
        """-> (template_blob, hashing_blob, template_nonce_offset, hashing_nonce_offset)"""
        out_t = ctypes.create_string_buffer(len(tmpl) + 4096)
        out_h = ctypes.create_string_buffer(1024)
        lt, lh = ctypes.c_size_t(len(out_t)), ctypes.c_size_t(len(out_h))
        ot, oh = ctypes.c_size_t(), ctypes.c_size_t()
        if self.lib.bcn_prepare_template(tmpl, len(tmpl), currency_id, out_t, ctypes.byref(lt),
                                         out_h, ctypes.byref(lh), ctypes.byref(ot), ctypes.byref(oh)) != 0:
            raise self._err()
        return out_t.raw[:lt.value], out_h.raw[:lh.value], ot.value, oh.value

    def hashing_blob_from_json(self, raw_header_json, currency_id):
        out_h = ctypes.create_string_buffer(1024)
        lh = ctypes.c_size_t(len(out_h))
        if self.lib.bcn_hashing_blob_from_json(raw_header_json.encode(), currency_id, out_h, ctypes.byref(lh)) != 0:
            raise self._err()
        return out_h.raw[:lh.value]

    def template_from_json(self, raw_header_json):
        out_t = ctypes.create_string_buffer(1 << 20)
        lt = ctypes.c_size_t(len(out_t))
        if self.lib.bcn_template_from_json(raw_header_json.encode(), out_t, ctypes.byref(lt)) != 0:
            raise self._err()
        return out_t.raw[:lt.value]

    def hashing_blob(self, tmpl, currency_id):
        out_h = ctypes.create_string_buffer(1024)
        lh = ctypes.c_size_t(len(out_h))
        if self.lib.bcn_hashing_blob(tmpl, len(tmpl), currency_id, out_h, ctypes.byref(lh)) != 0:
            raise self._err()
        return out_h.raw[:lh.value]

    def cn_slow_hash(self, data):
        out = ctypes.create_string_buffer(32)
        self.lib.bcn_cn_slow_hash(data, len(data), out)
        return out.raw

    def check_hash(self, hash32, difficulty):
        return bool(self.lib.bcn_check_hash(hash32, difficulty))
