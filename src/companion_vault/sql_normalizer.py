"""移植 shujuku 的 SQL 结构归一化，保留字面量与行注释。"""

from typing import List


_FULLWIDTH_TO_ASCII = str.maketrans("＝＞＜＋－＊／（），；　", "=><+-*/(),; ")


def normalize_sql_structure(sql: str) -> str:
    result: List[str] = []
    in_string = False
    in_comment = False
    index = 0
    while index < len(sql):
        char = sql[index]
        if in_comment:
            result.append(char)
            if char == "\n":
                in_comment = False
        elif in_string:
            result.append(char)
            if char == "'":
                if index + 1 < len(sql) and sql[index + 1] == "'":
                    result.append("'")
                    index += 1
                else:
                    in_string = False
        elif char == "'":
            in_string = True
            result.append(char)
        elif sql.startswith("--", index):
            in_comment = True
            result.append(char)
        else:
            result.append(char.translate(_FULLWIDTH_TO_ASCII))
        index += 1
    return "".join(result)
