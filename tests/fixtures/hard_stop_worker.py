"""Project-owned subprocess fixture. The parent kills ONLY this child handle."""

import asyncio
from pathlib import Path
import sys

import yaml

from omnisignal.connectors.durable import run_connector_durably
from omnisignal.contracts import ConnectorSpec
from omnisignal.testing import FixtureConnector


async def main():
    database, marker = Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve()
    stage = sys.argv[3]
    assert database.parent == marker.parent and stage in {"after_commit", "before_commit"}
    spec_data = yaml.safe_load((Path(__file__).parents[2] / "examples/connectors/rest.yaml").read_text(encoding="utf-8"))
    spec_data["pagination"].update(page_size=1, max_pages=10)
    spec = ConnectorSpec.model_validate(spec_data)

    class PausingFixture(FixtureConnector):
        calls = 0

        async def barrier(self):
            # Reaching this file means the exact transaction boundary was hit.
            marker.write_text("ready", encoding="ascii")
            await asyncio.Event().wait()

        async def collect(self, request):
            self.calls += 1
            if self.calls == 2 and stage == "after_commit":
                await self.barrier()
            return await super().collect(request)

        async def sync_deletions(self, checkpoint=None):
            # upsert_records has flushed the second batch but not committed it.
            if self.calls == 2 and stage == "before_commit":
                await self.barrier()
            return await super().sync_deletions(checkpoint)

    await run_connector_durably(
        PausingFixture(spec, [{"id": str(i), "text": f"fixture {i}"} for i in range(3)]),
        database_url=f"sqlite+pysqlite:///{database.as_posix()}",
        database_target="sqlite://local/hard-stop.db",
        initialize_schema=False,
    )


if __name__ == "__main__":
    asyncio.run(main())
