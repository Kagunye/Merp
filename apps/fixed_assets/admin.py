from django.contrib import admin
from .models import AssetCategory, FixedAsset, AssetDepreciation, AssetDisposal

admin.site.register(AssetCategory)
admin.site.register(FixedAsset)
admin.site.register(AssetDepreciation)
admin.site.register(AssetDisposal)
