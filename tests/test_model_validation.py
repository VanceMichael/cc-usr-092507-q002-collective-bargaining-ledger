import unittest

from collective_bargaining_ledger.model import Package, canonical_dumps, text_hash
from collective_bargaining_ledger.validation import (
    redact_content,
    redact_statement,
    validate_package,
)


class CanonicalTextTest(unittest.TestCase):
    def test_equivalent_objects_share_hash(self):
        a = {"b": 1, "a": [1, 2, {"x": "中文"}]}
        b = {"a": [1, 2, {"x": "中文"}], "b": 1}
        self.assertEqual(canonical_dumps(a), canonical_dumps(b))
        self.assertEqual(text_hash(a), text_hash(b))

    def test_single_byte_difference_conflicts(self):
        self.assertNotEqual(text_hash({"wage": 3000}), text_hash({"wage": 3001}))
        # 全角/半角、多余空白都必须暴露为异文
        self.assertNotEqual(canonical_dumps({"n": "a b"}),
                            canonical_dumps({"n": "a　b"}))

    def test_package_requires_all_three_kinds(self):
        with self.assertRaises(ValueError):
            Package(clauses={"wages": {}, "hours": {}})
        with self.assertRaises(ValueError):
            Package(clauses={"wages": {}, "hours": {}, "benefits": {},
                             "bonus": {}})


class LinkageTest(unittest.TestCase):
    DOC = {
        "clauses": {
            "wages": {"monthly_min": 3000, "monthly_raise_pct": 5},
            "hours": {"weekly_max": 40, "overtime_monthly_max": 30},
            "benefits": {"monthly_cost_person": 100},
        },
        "assumptions": {
            "headcount": 100,
            "monthly_capacity": 200_000,
            "legal_monthly_min": 2690,
            "legal_weekly_max": 40,
            "legal_overtime_monthly_max": 36,
        },
        "bases": {"current_monthly_wage": 6000},
        "note": "",
    }

    def test_balanced_package_passes_as_a_whole(self):
        self.assertEqual(validate_package(self.DOC), [])

    def test_below_legal_minimum_is_rejected(self):
        doc = _clone(self.DOC)
        doc["clauses"]["wages"]["monthly_min"] = 2000
        codes = {v["code"] for v in validate_package(doc)}
        self.assertIn("below_legal_min", codes)

    def test_overtime_ceiling_is_rejected(self):
        doc = _clone(self.DOC)
        doc["clauses"]["hours"]["overtime_monthly_max"] = 40
        codes = {v["code"] for v in validate_package(doc)}
        self.assertIn("over_legal_overtime", codes)

    def test_wages_and_benefits_counted_together_against_capacity(self):
        # 工资涨幅 5%（人均 300）+ 福利 2000，远超 20 万承受力
        doc = _clone(self.DOC)
        doc["clauses"]["benefits"]["monthly_cost_person"] = 2000
        violations = validate_package(doc)
        self.assertTrue(any(v["code"] == "capacity_exceeded" for v in violations),
                        violations)
        # 只把福利降下来而工资不动仍可能超标：涨幅 40% 单项即 24 万
        doc2 = _clone(self.DOC)
        doc2["clauses"]["benefits"]["monthly_cost_person"] = 0
        doc2["clauses"]["wages"]["monthly_raise_pct"] = 40
        self.assertTrue(any(v["code"] == "capacity_exceeded"
                            for v in validate_package(doc2)))

    def test_missing_clause_kind_blocks_everything(self):
        doc = _clone(self.DOC)
        del doc["clauses"]["benefits"]
        violations = validate_package(doc)
        self.assertEqual([v["code"] for v in violations], ["clause_missing"])

    def test_accepted_bases_must_align(self):
        doc = _clone(self.DOC)
        violations = validate_package(
            doc, accepted_bases={"worker": {"current_monthly_wage": 5000}})
        self.assertTrue(any(v["code"] == "bases_diverge" for v in violations))
        # 与已接受口径一致时无违规
        self.assertEqual(validate_package(
            doc, accepted_bases={"worker": {"current_monthly_wage": 6000}}), [])

    def test_text_only_clauses_skip_numeric_rules(self):
        doc = {
            "clauses": {
                "wages": {"text": "计件单价另行附表"},
                "hours": {"text": "综合工时制"},
                "benefits": {"text": "节日慰问"},
            },
            "assumptions": {},
            "bases": {},
            "note": "",
        }
        self.assertEqual(validate_package(doc), [])


class RedactionTest(unittest.TestCase):
    def test_owner_sees_everything_counterparty_sees_mask(self):
        content = {"summary": "支持涨薪", "sensitive": "家庭困难说明",
                   "nested": {"phone": "13800000000", "ok": "公开"}}
        other = redact_content(content, "company", "worker")
        self.assertEqual(other["summary"], "支持涨薪")
        self.assertEqual(other["sensitive"], "［依角色脱敏］")
        self.assertEqual(other["nested"]["phone"], "［依角色脱敏］")
        self.assertEqual(other["nested"]["ok"], "公开")
        self.assertEqual(redact_content(content, "worker", "worker"), content)

    def test_statement_redaction_hides_identity_from_counterparty(self):
        stmt = {"kind": "personal_statement", "side": "worker",
                "mandate_id": "m-1", "content": {"sensitive": "x", "k": 1}}
        view = redact_statement(stmt, "company")
        self.assertNotIn("mandate_id", view)
        self.assertEqual(view["content"]["sensitive"], "［依角色脱敏］")
        # 普通协商陈述不脱敏
        normal = {"kind": "demands", "side": "worker",
                  "mandate_id": "m-1", "content": {"x": 1}}
        self.assertEqual(redact_statement(normal, "company"), normal)


def _clone(value):
    import json
    return json.loads(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
