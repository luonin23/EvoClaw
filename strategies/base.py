"""策略基类。"""


class Strategy:
    mode = "base"

    def __init__(self, client, db, config):
        self.client = client
        self.db = db
        self.config = config

    async def step(self):
        """每次主循环调用一次。"""
        raise NotImplementedError

    async def status(self) -> dict:
        """返回给前端的策略状态。"""
        return {}
