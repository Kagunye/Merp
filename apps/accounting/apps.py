from django.apps import AppConfig


class AccountingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"
    verbose_name = "Accounting"

    def ready(self):
        """Release a settled document's unrealised exchange as it is settled.

        Receipts, supplier payments, credit and debit notes all end by saving
        the invoice or bill they settle, so listening for that save is the one
        place every way of settling a foreign document passes through — and it
        keeps the exchange bookkeeping here, beside the revaluation it belongs
        to, instead of in each of those screens.
        """
        from django.db.models.signals import post_save

        from apps.procurement.models import Bill
        from apps.sales.models import Invoice

        def settled(sender, instance, raw=False, **kwargs):
            if raw:
                return
            from apps.accounting.revaluation import on_document_saved
            on_document_saved(instance)

        post_save.connect(settled, sender=Invoice, weak=False,
                          dispatch_uid="accounting.revaluation.invoice_settled")
        post_save.connect(settled, sender=Bill, weak=False,
                          dispatch_uid="accounting.revaluation.bill_settled")
