import unittest

import fdroid_catalog as catalog


class CatalogTests(unittest.TestCase):
    def test_github_project_normalizes_deep_url(self):
        self.assertEqual(
            catalog.github_project(["https://github.com/Owner/Project.git/tree/main"]),
            ("https://github.com/Owner/Project", "Owner", "Project"),
        )

    def test_parse_v2(self):
        data = {
            "repo": {"name": {"en-US": "Test Repo"}},
            "packages": {
                "org.example.app": {
                    "metadata": {
                        "name": {"en-US": "Example"},
                        "summary": {"en-US": "<b>Useful</b> app"},
                        "sourceCode": "https://github.com/example/app",
                        "license": "GPL-3.0-only",
                        "categories": ["Internet"],
                        "lastUpdated": 1700000000000,
                    },
                    "versions": {
                        "old": {"manifest": {"versionName": "1.0", "versionCode": 1}},
                        "new": {
                            "added": 1700000000000,
                            "manifest": {
                                "versionName": "2.0", "versionCode": 2,
                                "usesSdk": {"minSdkVersion": 24},
                            },
                        },
                    },
                }
            },
        }
        repo_name, records = catalog.parse_v2(data, "fallback", "https://repo", "en-US")
        self.assertEqual(repo_name, "Test Repo")
        self.assertEqual(len(records), 1)
        app = records[0]
        self.assertEqual(app["name"], "Example")
        self.assertEqual(app["summary"], "Useful app")
        self.assertEqual(app["latest_version"], "2.0")
        self.assertEqual(app["latest_version_code"], 2)
        self.assertEqual(app["min_sdk"], 24)
        self.assertEqual(app["github_owner"], "example")

    def test_parse_v1(self):
        data = {
            "repo": {"name": "Legacy"},
            "apps": [{
                "packageName": "org.example.legacy",
                "name": "Legacy App",
                "summary": "Works",
                "sourceCode": "https://codeberg.org/example/legacy",
                "suggestedVersionName": "3.1",
                "suggestedVersionCode": "31",
            }],
        }
        repo_name, records = catalog.parse_v1(data, "fallback", "https://legacy", "en-US")
        self.assertEqual(repo_name, "Legacy")
        self.assertEqual(records[0]["package_id"], "org.example.legacy")
        self.assertEqual(records[0]["latest_version_code"], 31)
        self.assertEqual(records[0]["source_host"], "codeberg.org")

    def test_merge_preserves_repositories(self):
        first = catalog.make_record("app.id", {"name": "App"}, {}, "One", "https://one", "en-US")
        second = catalog.make_record("app.id", {"name": "App", "license": "MIT"}, {}, "Two", "https://two", "en-US")
        merged = catalog.merge_records([
            {"records": [first]},
            {"records": [second]},
        ])
        self.assertEqual(merged[0]["license"], "MIT")
        self.assertEqual([item["name"] for item in merged[0]["repositories"]], ["One", "Two"])


if __name__ == "__main__":
    unittest.main()
