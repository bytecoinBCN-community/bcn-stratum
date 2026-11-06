// Thin C ABI over bytecoin's own block serialization and CryptoNight,
// so the Python stratum bridge never has to re-implement the v4 block format.
#include <cstring>
#include <stdexcept>
#include "Core/CryptoNoteTools.hpp"
#include "Core/Difficulty.hpp"
#include "CryptoNote.hpp"
#include "common/JsonValue.hpp"
#include "crypto/crypto.hpp"
#include "seria/BinaryInputStream.hpp"
#include "seria/BinaryOutputStream.hpp"
#include "seria/JsonInputStream.hpp"

using namespace cn;

static thread_local std::string last_error;

static bool put(const common::BinaryArray &src, uint8_t *dst, size_t *cap) {
	if (src.size() > *cap)
		return false;
	std::memcpy(dst, src.data(), src.size());
	*cap = src.size();
	return true;
}

static size_t first_diff(const common::BinaryArray &a, const common::BinaryArray &b) {
	if (a.size() != b.size())
		throw std::runtime_error("nonce changed blob length");
	for (size_t i = 0; i != a.size(); ++i)
		if (a[i] != b[i])
			return i;
	throw std::runtime_error("nonce not found in blob");
}

static common::BinaryArray hashing_data(const BlockTemplate &bt, const Hash &currency_id) {
	return get_block_pow_hashing_data(bt, get_body_proxy_from_template(bt), currency_id);
}

extern "C" {

const char *bcn_last_error() { return last_error.c_str(); }

// Takes blocktemplate_blob from get_block_template, puts the solo-mining merge-mining tag
// into the root block (as minerd does) and returns the final template blob, the
// PoW hashing blob and where the 4-byte root nonce sits in each of them.
int bcn_prepare_template(const uint8_t *tmpl, size_t tmpl_len, const uint8_t *currency_id32, uint8_t *out_tmpl,
    size_t *out_tmpl_len, uint8_t *out_hash, size_t *out_hash_len, size_t *tmpl_nonce_off, size_t *hash_nonce_off) {
	try {
		BlockTemplate bt;
		seria::from_binary(bt, common::BinaryArray(tmpl, tmpl + tmpl_len));
		if (!bt.is_merge_mined())
			throw std::runtime_error("unexpected block major version " + std::to_string(bt.major_version));
		Hash cid;
		std::memcpy(cid.data, currency_id32, sizeof(cid.data));
		set_root_extra_to_solo_mining_tag(bt);

		std::memset(bt.root_block.nonce, 0, 4);
		const auto t0 = seria::to_binary(bt);
		const auto h0 = hashing_data(bt, cid);
		std::memset(bt.root_block.nonce, 0xff, 4);
		const auto t1 = seria::to_binary(bt);
		const auto h1 = hashing_data(bt, cid);

		*tmpl_nonce_off = first_diff(t0, t1);
		*hash_nonce_off = first_diff(h0, h1);
		if (!put(t0, out_tmpl, out_tmpl_len) || !put(h0, out_hash, out_hash_len))
			throw std::runtime_error("output buffer too small");
		return 0;
	} catch (const std::exception &ex) {
		last_error = ex.what();
		return -1;
	}
}

// PoW hashing blob of an already mined block, given raw_header JSON from get_raw_block.
// Used only to self-test against real network blocks.
int bcn_hashing_blob_from_json(const char *raw_header_json, const uint8_t *currency_id32, uint8_t *out_hash,
    size_t *out_hash_len) {
	try {
		BlockTemplate bt;
		seria::from_json_value(bt, common::JsonValue::from_string(raw_header_json));
		Hash cid;
		std::memcpy(cid.data, currency_id32, sizeof(cid.data));
		if (!put(hashing_data(bt, cid), out_hash, out_hash_len))
			throw std::runtime_error("output buffer too small");
		return 0;
	} catch (const std::exception &ex) {
		last_error = ex.what();
		return -1;
	}
}

// Test support: binary blocktemplate_blob of a mined block given its raw_header JSON,
// and the PoW hashing blob of a template exactly as it is (no root block reset).
int bcn_template_from_json(const char *raw_header_json, uint8_t *out_tmpl, size_t *out_tmpl_len) {
	try {
		BlockTemplate bt;
		seria::from_json_value(bt, common::JsonValue::from_string(raw_header_json));
		if (!put(seria::to_binary(bt), out_tmpl, out_tmpl_len))
			throw std::runtime_error("output buffer too small");
		return 0;
	} catch (const std::exception &ex) {
		last_error = ex.what();
		return -1;
	}
}

int bcn_hashing_blob(const uint8_t *tmpl, size_t tmpl_len, const uint8_t *currency_id32, uint8_t *out_hash,
    size_t *out_hash_len) {
	try {
		BlockTemplate bt;
		seria::from_binary(bt, common::BinaryArray(tmpl, tmpl + tmpl_len));
		Hash cid;
		std::memcpy(cid.data, currency_id32, sizeof(cid.data));
		if (!put(hashing_data(bt, cid), out_hash, out_hash_len))
			throw std::runtime_error("output buffer too small");
		return 0;
	} catch (const std::exception &ex) {
		last_error = ex.what();
		return -1;
	}
}

void bcn_cn_slow_hash(const uint8_t *data, size_t len, uint8_t *out32) {
	static thread_local crypto::CryptoNightContext ctx;
	const Hash h = ctx.cn_slow_hash(data, len);
	std::memcpy(out32, h.data, sizeof(h.data));
}

int bcn_check_hash(const uint8_t *hash32, uint64_t difficulty) {
	Hash h;
	std::memcpy(h.data, hash32, sizeof(h.data));
	return check_hash(h, difficulty) ? 1 : 0;
}
}
