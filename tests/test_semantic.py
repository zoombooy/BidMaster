"""语义判定引擎测试：scope 抽取 + 保守匹配（真实国网口径）。"""
from bidmaster.scoring.scope import extract_scope
from bidmaster.scoring.matcher import match_performance

K1_TEXT = "开标前5年内：换流站土建A包：企业具有±800kV换流站土建A包业绩的，如有业绩得3分，如无得0分"
K3_TEXT = "开标前5年内企业具有1000kV变电站工程监理业绩的，得3分"


class TestScopeExtraction:
    def test_k1_full(self):
        scope = extract_scope(K1_TEXT)
        assert scope["voltage_levels"] == ["800KV"]
        assert scope["facilities"] == ["换流站"]
        assert "土建" in scope["work_types"]
        assert scope["subdivision"] == "A包"

    def test_k3(self):
        scope = extract_scope(K3_TEXT)
        assert scope["voltage_levels"] == ["1000KV"]
        assert scope["facilities"] == ["变电站"]
        assert "监理" in scope["work_types"]

    def test_no_scope(self):
        assert extract_scope("具有丰富的工程实施经验") == {}

    def test_definition_sentence(self):
        scope = extract_scope("本招标文件所称同类工程是指220kV及以上输变电工程。")
        assert "220kV及以上输变电工程" in scope.get("definition", "")


class TestConservativeMatcher:
    def _scope_k1(self):
        return extract_scope(K1_TEXT)

    def test_match(self):
        r = match_performance(
            "±800kV某某换流站土建施工工程，合同金额12000万元，2024年6月竣工",
            self._scope_k1(), years=5)
        assert r["verdict"] == "match"
        assert r["confidence"] >= 0.8

    def test_voltage_miss(self):
        r = match_performance(
            "500kV某某变电站新建工程，合同金额6000万元，2022年12月竣工",
            self._scope_k1(), years=5)
        assert r["verdict"] == "no_match"
        states = {x["dim"]: x["state"] for x in r["report"]}
        assert states["voltage_levels"] == "miss"

    def test_fuzzy_desc_uncertain(self):
        r = match_performance("参与多项大型电网工程建设，具有丰富的实施经验",
                              self._scope_k1(), years=5)
        assert r["verdict"] == "uncertain"

    def test_amount_gate(self):
        scope = {"work_types": ["房屋建筑"]}
        r = match_performance("某某医院门诊综合楼房屋建筑工程，2024年3月竣工",
                              scope, amount_min=50_000_000)
        assert r["verdict"] == "uncertain"  # 无金额信息 → 保守
        r2 = match_performance("某某医院门诊综合楼房屋建筑工程，合同金额6000万元",
                               scope, amount_min=50_000_000)
        assert r2["verdict"] == "match"

    def test_work_type_miss(self):
        scope = {"work_types": ["房屋建筑"]}
        r = match_performance("1000kV某某变电站电气安装工程，合同金额8000万元",
                              scope, amount_min=50_000_000)
        assert r["verdict"] == "no_match"

    def test_subdivision_explicit_conflict(self):
        scope = {"voltage_levels": ["800KV"], "facilities": ["换流站"],
                 "work_types": ["土建"], "subdivision": "A包"}
        r = match_performance("±800kV某某换流站土建B包工程", scope)
        states = {x["dim"]: x["state"] for x in r["report"]}
        assert states["subdivision"] == "miss"
        assert r["verdict"] == "no_match"

    def test_empty_scope_uncertain(self):
        r = match_performance("任意业绩描述", {})
        assert r["verdict"] == "uncertain"
