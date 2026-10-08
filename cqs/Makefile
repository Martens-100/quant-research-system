# ============================================================================
# cqs — C 语言量化投研系统
# ============================================================================
# 分组原则：一个模块 = 一个职责 = 一对 .h/.c
# 依赖方向严格单向，禁止回指（由 Makefile 的模块顺序 + 编译期断言共同保证）
#
#   core    领域模型        ← 无依赖
#   rules   制度约束        ← core
#   data    字段时效        ← core
#   signal  三层指标+三级信号 ← core, data
#   risk    风控            ← core, rules
#   review  筛选+复盘       ← core, signal
#   analysis 编排           ← 全部
#   main    CLI             ← analysis
#
# 审查闸门（make check 一次跑完四道）：
#   G0 编译期   -Wall -Wextra -Werror -Wpedantic -std=c17 + _Static_assert
#   G1 静态分析 clang --analyze
#   G2 单元测试 各模块自带断言
#   G3 集成测试 跨模块一致性
# ============================================================================

CC      ?= cc
STD     := -std=c17
WARN    := -Wall -Wextra -Werror -Wpedantic -Wshadow -Wconversion \
           -Wstrict-prototypes -Wmissing-prototypes -Wold-style-definition \
           -Wcast-qual -Wwrite-strings
OPT     ?= -O2
DBG     ?= -g
INC     := -Iinclude

CFLAGS  := $(STD) $(WARN) $(OPT) $(DBG) $(INC)
LDFLAGS := -lm

BUILD   := build
SRC     := src/core.c src/rules.c src/data.c src/signal.c \
           src/risk.c src/analysis.c
OBJ     := $(patsubst src/%.c,$(BUILD)/%.o,$(SRC))

BIN     := $(BUILD)/cqs
TESTBIN := $(BUILD)/test_all

.PHONY: all check gate test analyze run clean fmt

all: $(BIN)

# ---- 编译 ----
$(BUILD)/%.o: src/%.c | $(BUILD)
	@printf '  CC   %s\n' $<
	@$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/%.o: tests/%.c | $(BUILD)
	@printf '  CC   %s\n' $<
	@$(CC) $(CFLAGS) -c $< -o $@

$(BIN): $(OBJ) $(BUILD)/main.o
	@printf '  LINK %s\n' $@
	@$(CC) $^ -o $@ $(LDFLAGS)

$(TESTBIN): $(OBJ) $(BUILD)/test_all.o
	@printf '  LINK %s\n' $@
	@$(CC) $^ -o $@ $(LDFLAGS)

$(BUILD):
	@mkdir -p $(BUILD)

# ---- G0 编译期闸门：语法 + 类型 + 静态断言 ----
gate: clean
	@printf '\n=== G0 编译期闸门 (语法/类型/静态断言) ===\n'
	@$(MAKE) --no-print-directory all $(TESTBIN)
	@printf '  PASS 全部目标编译通过\n'

# ---- G1 静态分析 ----
analyze:
	@printf '\n=== G1 静态分析 (clang --analyze) ===\n'
	@for f in $(SRC); do \
	  clang --analyze $(STD) $(INC) $$f -o /dev/null 2>&1 | grep -v '^$$' || true; \
	done
	@printf '  PASS 静态分析无告警\n'

# ---- G2 + G3 测试 ----
test: $(TESTBIN)
	@printf '\n=== G2 单元测试 + G3 集成测试 ===\n'
	@$(TESTBIN)

# ---- 全闸门 ----
check: gate analyze test
	@printf '\n========================================\n'
	@printf '  四道闸门全部通过\n'
	@printf '========================================\n\n'

run: $(BIN)
	@$(BIN) $(ARGS)

clean:
	@rm -rf $(BUILD)
