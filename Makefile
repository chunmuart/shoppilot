HARNESS_RUN_DIR ?= eval/verification/harness-local-$(shell date +%Y%m%d-%H%M%S)
HARNESS_SUITE ?= eval/harness/v1/suite.json
HARNESS_STRATEGIES ?= current,candidate
HARNESS_PYTHON ?= uv run python

.PHONY: eval-harness eval-harness-contracts eval-harness-dev eval-harness-release

# 每次 Harness 修改：安全契约 + 完整真实模型冒烟 + 同源码证据检查。
eval-harness:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile smoke --strategies $(HARNESS_STRATEGIES) --output $(HARNESS_RUN_DIR)
	$(HARNESS_PYTHON) -m scripts.eval.harness verify $(HARNESS_RUN_DIR)

eval-harness-contracts:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile contracts --output $(HARNESS_RUN_DIR)

eval-harness-dev:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile dev --strategies $(HARNESS_STRATEGIES) --output $(HARNESS_RUN_DIR)

eval-harness-release:
	$(HARNESS_PYTHON) -m scripts.eval.harness run --suite $(HARNESS_SUITE) --profile release --strategies $(HARNESS_STRATEGIES) --output $(HARNESS_RUN_DIR) --require-benefit
	$(HARNESS_PYTHON) -m scripts.eval.harness verify $(HARNESS_RUN_DIR) --minimum release
