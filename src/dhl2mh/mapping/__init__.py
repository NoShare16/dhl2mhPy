"""Translation between the external formats and the domain models.

* ``constants``   – service ids, property/group ids, match-code table
* ``plenty``      – Plenty API order → :class:`~dhl2mh.models.order.PlentyOrder`
* ``shopware``    – Shopware order/product data → order items
* ``akeneo``      – Akeneo PIM values → model name
* ``xml_builder`` – domain order → DHL DeliverIT XML
"""
