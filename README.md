# 设备安全通告采集与适用批次处置

合成机器人公告、设备台账和维修工作样例。日期采用ISO格式，序列号范围按字符串闭区间理解。

All example identifiers and values are synthetic. CSV files use UTF-8, the first row contains column names, and an empty cell means the source supplied no value.

## Sample files

- `notices.csv` contains 监管与厂商安全通告版本及适用范围。 Columns are `notice`, `issuer`, `model`, `serial_from`, `serial_to`, `firmware`, `revision`, `state`.
- `assets.csv` contains 客户在用设备及固件清单。 Columns are `asset`, `customer`, `model`, `serial`, `firmware`, `site`.
- `work.csv` contains 维修检查及现场回执。 Columns are `case_id`, `asset`, `notice`, `action`, `state`, `completed`.

Python 3 standard library is sufficient to inspect the sample files. From this directory, run `python3 -m unittest discover -s tests -v` to verify only that the supplied files are well-formed and their sample references resolve.
