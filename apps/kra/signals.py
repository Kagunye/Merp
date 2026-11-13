"""Hooks: when a document is booked, queue it for KRA once its transaction
commits (a rolled-back sale is never fiscalised). A KRA problem never breaks
the sale: it is logged on the submission and the queue retries it."""
import logging

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

log = logging.getLogger("apps.kra")


def _later(company, fn, *args):
    from . import services

    if company is None or not services.is_on(company):
        return

    def run():
        try:
            fn(*args)
        except Exception:   # noqa: BLE001 - reported, never raised into the sale
            log.exception("eTIMS: could not queue %s", args[:1])
            return
        services.send_soon(company) if transaction.get_connection().in_atomic_block else _send(company)
    transaction.on_commit(run)


def _send(company):
    from . import services
    try:
        if services.settings_for(company).auto_send:
            services.process(company, limit=50)
    except Exception:       # noqa: BLE001
        log.exception("eTIMS: sending failed for %s", company)


def document_booked(document):
    """Called by the ledger when an invoice, credit note, till ticket or till
    refund has been booked (the ledger links entries with UPDATEs, which fire
    no save signal, so this is called explicitly from gl_service)."""
    from . import services
    label = document._meta.label
    if label == "sales.Invoice":
        _later(document.company, services.queue_invoice, document)
    elif label == "pos.POSSale":
        _later(document.session.company, services.queue_pos_sale, document)


@receiver(post_save, sender="inventory.Product")
def product_saved(sender, instance, **kwargs):
    from . import services
    _later(instance.company, services.queue_item, instance)


@receiver(post_save, sender="inventory.StockMovement")
def stock_moved(sender, instance, created, **kwargs):
    if created:
        from . import services
        _later(instance.product.company, services.queue_stock_movement, instance)
