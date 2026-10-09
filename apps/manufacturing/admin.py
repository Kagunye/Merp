from django.contrib import admin
from .models import BillOfMaterials, BOMLine, WorkOrder, WorkOrderConsumption

admin.site.register(BillOfMaterials)
admin.site.register(BOMLine)
admin.site.register(WorkOrder)
admin.site.register(WorkOrderConsumption)
