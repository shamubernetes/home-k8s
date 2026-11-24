"""Run inside a candidate OpenViking image, with only disposable state."""

import asyncio
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zipfile import ZipFile

from openviking.models.embedder.base import DenseEmbedderBase, EmbedResult
from openviking.server.dependencies import set_service
from openviking.server.identity import RequestContext, Role
from openviking.service.core import OpenVikingService
from openviking.storage.ovpack.operations import (
    base_name_from_entries,
    is_backup_package,
    read_dense_vectors,
    read_manifest,
    validate_manifest_content,
    validate_manifest_root_matches_zip,
)
from openviking_cli.exceptions import InvalidArgumentError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config import OpenVikingConfigSingleton
from openviking_cli.utils.config.embedding_config import EmbeddingConfig



class FixtureEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="synthetic")

    def get_dimension(self):
        return 8

    def embed(self, text, is_query=False):
        return EmbedResult(dense_vector=[0.1] * 8)


async def check_backup(work):
    OpenVikingConfigSingleton.initialize(config_dict={
        "storage": {
            "workspace": str(work),
            "agfs": {"backend": "local"},
            "vectordb": {"backend": "local"},
        },
        "embedding": {"dense": {
            "provider": "openai", "model": "synthetic", "dimension": 8,
            "api_key": token_urlsafe(24),
        }},
    })
    owner = UserIdentifier.the_default_user("fixture-owner")
    admin = RequestContext(user=owner, role=Role.ADMIN)
    output = work / "backup.ovpack"
    with patch.object(EmbeddingConfig, "get_embedder", lambda _: FixtureEmbedder()):
        service = OpenVikingService(path=str(work), user=owner)
        await service.initialize()
        set_service(service)
        try:
            queues = await service.resources.wait_processed()
            if any(q["error_count"] for q in queues.values()):
                raise RuntimeError("OpenViking bootstrap queue reported an error")
            # Do not repair the fixture. The candidate must export the state it
            # generated itself, including any files seeded on account bootstrap.
            try:
                await service.pack.backup_ovpack(str(output), ctx=admin, include_vectors=True)
            except InvalidArgumentError as error:
                if error.details.get("missing_record_count", 0) > 0:
                    raise SystemExit("OpenViking startup left an incomplete vector index") from None
                raise
            with ZipFile(output) as archive:
                entries = archive.infolist()
                root = base_name_from_entries(entries)
                manifest = read_manifest(archive, root)
                validate_manifest_root_matches_zip(manifest, root)
                if not is_backup_package(manifest) or archive.testzip() is not None:
                    raise RuntimeError("Expected a valid native OpenViking backup package")
                records = validate_manifest_content(archive, manifest, entries, root)
                if not records or not read_dense_vectors(archive, manifest, root, records):
                    raise RuntimeError("Native backup did not include index records and dense vectors")
        finally:
            await service.close()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="ov-upgrade-") as tmp:
        asyncio.run(asyncio.wait_for(check_backup(Path(tmp)), timeout=90))
    print("OpenViking startup produced a complete native vector-inclusive backup")
