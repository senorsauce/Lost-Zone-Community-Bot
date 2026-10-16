import unittest

from dayz import DayzCog


class DayzInventorySyncTests(unittest.TestCase):
    def test_detects_radio_items_in_inventory_payloads(self):
        cog = DayzCog(None)

        self.assertTrue(cog.inventory_contains_radio(["flashlight", "radio"]))
        self.assertTrue(cog.inventory_contains_radio(["walkie-talkie", "matches"]))
        self.assertFalse(cog.inventory_contains_radio(["knife", "bandage"]))

    def test_extracts_item_names_from_common_api_payloads(self):
        cog = DayzCog(None)

        payload = {"inventory": [{"name": "radio"}, {"name": "battery"}]}
        self.assertEqual(cog.extract_inventory_items(payload), ["radio", "battery"])

        payload = {"items": ["flashlight", "walkie-talkie"]}
        self.assertEqual(cog.extract_inventory_items(payload), ["flashlight", "walkie-talkie"])


if __name__ == "__main__":
    unittest.main()
