"""YuanHao 2026-11-12 taste, locked into the shared av_auto review prompt.

DeepSeek-flash, gpt-6-luna, and the Grok tiebreaker all receive REVIEW_SYSTEM.
These tests never call Magpie.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import av_auto  # noqa: E402

# The 12-title set is calibration context, not a memorized answer key.
CASE_CODES = (
    "ABP-838",
    "FSDSS-393",
    "IPZ-385",
    "JUFD-634",
    "JUL-802",
    "BAB-042",
    "CAWD-676",
    "DDFF-045",
    "IPX-219",
    "IPZ-687",
    "IPZZ-859",
    "JUR-810",
)


class ReviewCriteriaTests(unittest.TestCase):
    def setUp(self):
        self.prompt = av_auto.REVIEW_SYSTEM
        self.source = Path(av_auto.__file__).read_text(encoding="utf-8")

    def test_dual_model_and_grok_share_the_prompt(self):
        self.assertEqual(
            av_auto.REVIEW_MODELS,
            ("deepseek/deepseek-flash", "chatgpt-api/gpt-6-luna"),
        )
        self.assertEqual(av_auto.GROK_TIEBREAK_MODEL, "cursor/grok-4.7")
        self.assertIn(
            "_chat_retry(llm, model, images, REVIEW_SYSTEM, user, REVIEW_TO, 800)",
            self.source,
        )
        self.assertIn(
            "return _post_magpie_chat(cfg, model, images, REVIEW_SYSTEM, user_text, REVIEW_TO, 800)",
            self.source,
        )

    def test_hit_names(self):
        for key in (
            "dry_thin",
            "looks_old",
            "poor_skin",
            "not_pretty",
            "soft_chest",
            "implants",
            "grimace",
        ):
            self.assertIn(key, self.prompt)
        self.assertIn("hits 只能用上述英文维度名，可多项。", self.prompt)

    def test_taste_overrides_a_high_beauty_impression(self):
        self.assertIn(
            "皮肤白、年轻感、身材有曲线，或观感上的高颜值，都不能抵消任一较明确的命中。",
            self.prompt,
        )
        self.assertIn(
            "verdict=fail：至少一项在图中较明确，包括不漂亮、显老、胸部不够挺拔、假胸，或符合上面口径的狰狞。高颜值观感不能改成 pass。",
            self.prompt,
        )

    def test_not_pretty_is_its_own_fail(self):
        self.assertIn("not_pretty（不漂亮：", self.prompt)
        self.assertIn("仍然可以因为不好看单独 fail。", self.prompt)
        self.assertIn("不要把「挑不出干扁/显老/皮肤差/狰狞」当成 pass。", self.prompt)
        self.assertIn("已经明显好看、有吸引力的不要打", self.prompt)

    def test_looks_old_is_not_cancelled_by_fair_skin(self):
        self.assertIn("looks_old（显老：", self.prompt)
        self.assertIn("看起来比应有的年轻感老", self.prompt)
        self.assertIn("皮肤白或身材丰满不能抵消", self.prompt)
        self.assertIn("人妻/熟女/老片/蜜色不单独判负", self.prompt)

    def test_soft_chest_and_implants(self):
        self.assertIn("soft_chest（胸部不够挺拔：下垂、软塌、不够立。", self.prompt)
        self.assertIn("丰满或罩杯大不等于挺拔", self.prompt)
        self.assertIn("挺的、只是大的，不要打", self.prompt)
        self.assertIn("implants（假胸：", self.prompt)
        self.assertIn("一看就是假", self.prompt)
        self.assertIn("自然的大胸和挺拔的真胸不要打。看不准是不是假体就不要打", self.prompt)

    def test_grimace_spares_erotic_strain_and_still_fails_obvious_pain(self):
        self.assertNotIn("夸张白目/失神", self.prompt)
        self.assertIn(
            "「色不是难受」只指明显痛苦狰狞，不指色情高潮或 SM/调教里的用力脸。",
            self.prompt,
        )
        self.assertIn(
            "色情用力脸：皱眉、露齿、眯眼、张嘴、眉眼紧绷、嘴微张、闭眼、轻微失神或白眼。",
            self.prompt,
        )
        self.assertIn(
            "出现在性行为、捆绑、拘束、SM、调教、强制高潮里时，视为色，不打 grimace，不要 fail。",
            self.prompt,
        )
        self.assertIn("单帧或末段近景的紧皱露齿、眯眼张嘴、眉眼紧蹙嘴微张，不够。", self.prompt)
        self.assertIn(
            "痛苦狰狞脸：多帧都是夸张的痛苦皱眉张口，或五官痛到扭曲、哭喊；单独一帧已经明显是受苦而不是高潮，也要打 grimace 并 fail。",
            self.prompt,
        )
        self.assertIn("不要把这种脸改口说成快感。", self.prompt)
        self.assertIn(
            "不要因为题材是 SM 就把用力脸打成狰狞，也不要因为题材是凌辱或强奸就把已经明显痛苦的脸放过。",
            self.prompt,
        )

    def test_prompt_does_not_memorize_the_case_set(self):
        for code in CASE_CODES:
            self.assertNotIn(code, self.prompt)

    def test_output_contract_unchanged(self):
        self.assertIn(
            '{"verdict":"pass|fail|uncertain","hits":[],"reason":"一句短理由","confidence":"high|mid|low"}。',
            self.prompt,
        )
        self.assertIn("只使用有效抽帧；不要根据无效空景或纯字幕想象内容。", self.prompt)
        self.assertIn("スレンダー不要单独判成干扁", self.prompt)


if __name__ == "__main__":
    unittest.main()
