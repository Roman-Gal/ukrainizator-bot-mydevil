import unittest
from translator.config import Settings


class PrivateDatabaseConfigTests(unittest.TestCase):
    def test_settings_repr_omits_postgresql_password_and_api_secrets(self):
        settings = Settings(
            database="postgresql://user:private-db-password@localhost/example",
            token="123:private-telegram-token",
            secret="private-webhook-secret",
            api_key="private-translation-key",
        )
        for value in [settings.database, "private-db-password", settings.token, settings.secret, settings.api_key]:
            self.assertNotIn(value, repr(settings))
