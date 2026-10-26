from django.apps import AppConfig


class BankingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.banking"
    verbose_name = "Banking"

    def ready(self):
        """Let the ledger feed the bank book.

        Every posted entry that moves a bank or cash ledger account puts a line
        on the bank book of the bank account behind it, and a reversal takes it
        off again; see apps.banking.bankbook for the rules. Listening for the
        entry's save is the one place every module's posting passes through.
        """
        from django.db.models.signals import post_save

        from apps.accounting.models import JournalEntry

        from .bankbook import on_entry_saved

        post_save.connect(on_entry_saved, sender=JournalEntry, weak=False,
                          dispatch_uid="banking.bankbook.entry_saved")
