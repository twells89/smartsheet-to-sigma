import sys
from pathlib import Path
import unittest


LIB = Path(__file__).resolve().parents[1] / "scripts" / "lib"
sys.path.insert(0, str(LIB))

from formula_translation import normalize_formula, translate_formula


class FormulaTranslationTests(unittest.TestCase):
    def test_translates_same_row_scalar_formula(self):
        result = translate_formula("=IF([Status]@row = \"Open\", [Amount]@row * 2, 0)")
        self.assertTrue(result["supported"])
        self.assertEqual(
            result["sigma"],
            'If([Status] = "Open", [Amount] * 2, 0)',
        )

    def test_preserves_formula_tokens_inside_string_literals(self):
        result = translate_formula(
            '=IF([Status]@row = "Open: {Queue} [Amount]12 @row <> IF(", 1, 0)'
        )
        self.assertTrue(result["supported"])
        self.assertEqual(
            result["sigma"],
            'If([Status] = "Open: {Queue} [Amount]12 @row <> IF(", 1, 0)',
        )

    def test_rejects_aggregate_function_without_scalar_ast(self):
        result = translate_formula("=AVERAGE([Amount]@row, 10)")
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "UNSUPPORTED_FUNCTION")

    def test_rejects_cross_sheet_reference(self):
        result = translate_formula("=SUM({Budget Range})")
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "CROSS_SHEET_REFERENCE")

    def test_rejects_hierarchy_function(self):
        result = translate_formula("=SUM(CHILDREN())")
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "HIERARCHY_FUNCTION")

    def test_rejects_row_position_reference(self):
        result = translate_formula("=[Amount]12 + [Amount]13")
        self.assertFalse(result["supported"])
        self.assertEqual(result["reason"], "ROW_POSITION_REFERENCE")

    def test_normalizes_numbered_references_for_shape_comparison(self):
        self.assertEqual(
            normalize_formula("=[Amount]12 + [Tax]12"),
            "=[AMOUNT]#+[TAX]#",
        )


if __name__ == "__main__":
    unittest.main()
