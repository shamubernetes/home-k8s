"""Compose native/configuration capture with an explicit bound consumer roster.

This revalidates selected catalog/query evidence, never grants source authority,
writer cessation, destination access or whole-index production acceptance.
"""
import copy

from kopiur_elasticsearch_capture import SnapshotCapture
from kopiur_elasticsearch_catalog import SourceCatalog
from kopiur_elasticsearch_queries import ConsumerQueries, contract_digests, validate_queries
from kopiur_elasticsearch_escrow import EscrowError, _binding, _encoded, _same_binding, _validate_parts


class ConsumerBoundCapture:
    """Keep one caller-qualified roster coherent across every capture phase."""
    def __init__(self, binding, *, snapshot, queries):
        self.binding = _binding(binding)
        if (not isinstance(snapshot, SnapshotCapture) or not isinstance(queries, ConsumerQueries)
                or not isinstance(queries.catalog, SourceCatalog)
                or any(not _same_binding(value, self.binding)
                       for value in (snapshot.binding, queries.binding, queries.catalog.binding))
                or set(snapshot.indices) != set(queries.catalog.indices)):
            raise EscrowError('complete source-bound native/catalog/query composition required')
        self.expected = validate_queries(self.binding.get('source_queries'), self.binding)
        if _encoded(self.expected['contracts']) != _encoded(contract_digests(queries.contracts)):
            raise EscrowError('prepared consumer query contracts differ')
        self.snapshot, self.queries = snapshot, queries

    def checkpoint(self):
        # The authenticated adapter rechecks independent authority and lifetime
        # around credential I/O. ConsumerQueries brackets both result passes with
        # fresh catalog reads and guards. No metadata comparison grants authority.
        self.snapshot.check(self.binding)
        credentials = self.snapshot.authenticated_credentials(self.binding)
        actual = self.queries.capture(credentials)
        if _encoded(actual) != _encoded(self.expected):
            raise EscrowError('bound consumer evidence changed across capture/export')
        self.snapshot.check(self.binding)
        return True

    def call(self, operation, *args):
        self.checkpoint()
        try:
            value = operation(*args)
        except EscrowError:
            raise
        except Exception:
            raise EscrowError('bound consumer capture/export operation failed') from None
        self.checkpoint()
        return value

    def capture(self, read_configuration):
        """Read runtime and config through the supplied checkpoint-aware adapter.

        read_configuration receives binding, native, credentials and checkpoint.
        It must checkpoint each source read, preserving its own lifetime guard.
        """
        native = self.call(self.snapshot.native, copy.deepcopy(self.binding))
        credentials = self.call(self.snapshot.credentials, copy.deepcopy(self.binding))
        parts = self.call(read_configuration, copy.deepcopy(self.binding),
                          native, credentials, self.checkpoint)
        _validate_parts(self.binding, parts)
        self.checkpoint()
        return parts

    def encrypt(self, adapter, manifest, parts):
        """Keep live evidence checked around an explicitly approved exporter.

        The escrow contract validates receipt structure and manifest digest.
        This method does not qualify encryption or grant destination authority.
        """
        if (not isinstance(manifest, dict)
                or not _same_binding(manifest.get('binding'), self.binding)):
            raise EscrowError('consumer export manifest binding differs')
        _validate_parts(self.binding, parts)
        def export():
            try:
                return adapter(copy.deepcopy(manifest), copy.deepcopy(parts))
            except Exception:
                # External adapters may raise EscrowError with private details.
                # Keep their entire exception chain out of logs and receipts.
                raise EscrowError('bound consumer capture/export operation failed') from None
        return self.call(export)
