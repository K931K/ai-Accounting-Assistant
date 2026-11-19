"""导出 OpenAPI 契约文件，供 Apifox 导入。

用法（backend 目录）：
    python export_openapi.py

输出：docs/api/openapi.json
"""

import json
from pathlib import Path

# 导入 app.main 不会连接数据库：连接只在应用启动的 lifespan 里建立，
# 所以导出契约不需要 MySQL 正在运行。
from app.main import app  # noqa: E402

# 相对 backend/export_openapi.py 定位到仓库的 docs/api 目录。
OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi.json"


def main() -> None:
    """调用 app.openapi() 生成契约并写入 OUTPUT。

    app.openapi() 只遍历已注册的路由和 Pydantic 模型，不连接数据库，
    因此导出结果只反映接口定义，不依赖当前数据库里有没有数据。
    """
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(OUTPUT)


if __name__ == "__main__":
    main()
