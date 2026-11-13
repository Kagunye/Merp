from django.urls import path

from . import cron, views

app_name = "kra"

urlpatterns = [
    path("", views.home, name="home"),
    path("settings/", views.settings_edit, name="settings_edit"),
    path("settings/tax-types/", views.tax_map, name="settings_tax_map"),
    path("devices/", views.device_list, name="device_list"),
    path("devices/new/", views.device_edit, name="device_create"),
    path("devices/<uuid:pk>/", views.device_edit, name="device_edit"),
    path("devices/<uuid:pk>/initialise/", views.device_init, name="device_init"),
    path("codes/", views.codes, name="codes"),
    path("codes/sync/", views.codes_sync, name="codes_sync"),
    path("items/", views.items, name="items"),
    path("items/register/", views.item_register, name="item_register"),
    path("queue/", views.queue, name="queue"),
    path("queue/run/", views.queue_run, name="queue_run"),
    path("queue/<uuid:pk>/", views.queue_detail, name="queue_detail"),
    path("queue/<uuid:pk>/retry/", views.queue_retry, name="queue_retry"),
    path("purchases/", views.purchases, name="purchases"),
    path("purchases/fetch/", views.purchases_fetch, name="purchases_fetch"),
    path("purchases/<uuid:pk>/decide/", views.purchase_decide, name="purchase_decide"),
    path("imports/", views.imports, name="imports"),
    path("imports/fetch/", views.imports_fetch, name="imports_fetch"),
    path("imports/<uuid:pk>/decide/", views.import_decide, name="import_decide"),
    path("pin-check/", views.pin_check, name="pin_check"),
    path("notices/", views.notices, name="notices"),
    path("checklist/", views.checklist, name="checklist"),
    path("cron/", cron.run, name="cron"),
]
