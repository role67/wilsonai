"""
Скрипт для проверки работоспособности улучшенной версии.
Запускает все тесты и проверяет, что новые компоненты работают корректно.
"""

import sys
import subprocess
from pathlib import Path

# Исправление кодировки для Windows
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')


def print_header(text: str) -> None:
    """Печать заголовка."""
    print("\n" + "=" * 80)
    print(f"  {text}")
    print("=" * 80 + "\n")


def check_file_exists(filepath: str) -> bool:
    """Проверка существования файла."""
    path = Path(filepath)
    exists = path.exists()
    status = "[OK]" if exists else "[FAIL]"
    print(f"{status} {filepath}")
    return exists


def run_command(cmd: list[str], description: str) -> bool:
    """Запуск команды и проверка результата."""
    print(f"\n[RUN] {description}...")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            print(f"[OK] {description} - успешно")
            return True
        else:
            print(f"[FAIL] {description} - ошибка")
            print(f"Вывод: {result.stderr[:500]}")
            return False
    except subprocess.TimeoutExpired:
        print(f"[TIMEOUT] {description} - таймаут")
        return False
    except Exception as e:
        print(f"[ERROR] {description} - исключение: {e}")
        return False


def main() -> int:
    """Основная функция проверки."""
    
    print_header("ПРОВЕРКА УЛУЧШЕННОЙ ВЕРСИИ TELEGRAM AGENT")
    
    # Проверка новых файлов
    print_header("1. Проверка новых файлов")
    
    required_files = [
        "agent_state.py",
        "constants.py",
        "exceptions.py",
        "db_improved.py",
        "examples.py",
        "tests/test_agent.py",
        "requirements-dev.txt",
        "MIGRATION.md",
        "README_NEW.md",
        "IMPROVEMENTS_REPORT.md",
        "QUICKSTART.md",
        "FINAL_SUMMARY.md",
    ]
    
    all_files_exist = True
    for filepath in required_files:
        if not check_file_exists(filepath):
            all_files_exist = False
    
    if not all_files_exist:
        print("\n[FAIL] Не все файлы на месте!")
        return 1
    
    print("\n[OK] Все новые файлы на месте!")
    
    # Проверка импортов
    print_header("2. Проверка импортов")
    
    imports_to_check = [
        ("agent_state", "AgentState, get_state, TTLCache"),
        ("constants", "MESSAGES, MOD_CHAT_USERNAME"),
        ("exceptions", "ModelConnectionError, DatabaseConnectionError"),
    ]
    
    all_imports_ok = True
    for module, items in imports_to_check:
        try:
            exec(f"from {module} import {items}")
            print(f"[OK] from {module} import {items}")
        except ImportError as e:
            print(f"[FAIL] from {module} import {items} - ошибка: {e}")
            all_imports_ok = False
    
    if not all_imports_ok:
        print("\n[FAIL] Не все импорты работают!")
        return 1
    
    print("\n[OK] Все импорты работают!")
    
    # Проверка базовой функциональности
    print_header("3. Проверка базовой функциональности")
    
    try:
        from wilsonai.agent.state import AgentState, TTLCache
        
        # Тест AgentState
        state = AgentState()
        state.block_sending(10)
        assert state.is_send_blocked()
        print("[OK] AgentState работает")
        
        # Тест TTLCache
        cache = TTLCache(ttl_seconds=60.0)
        cache.set("test", "value")
        assert cache.get("test") == "value"
        print("[OK] TTLCache работает")
        
        # Тест констант
        from wilsonai.core.constants import MESSAGES
        assert "help" in MESSAGES
        print("[OK] Константы загружаются")
        
        # Тест исключений
        from wilsonai.core.exceptions import ModelConnectionError
        try:
            raise ModelConnectionError("test")
        except ModelConnectionError:
            print("[OK] Исключения работают")
        
    except Exception as e:
        print(f"[FAIL] Ошибка при проверке функциональности: {e}")
        return 1
    
    print("\n[OK] Базовая функциональность работает!")
    
    # Запуск тестов
    print_header("4. Запуск unit тестов")
    
    # Проверка наличия pytest
    try:
        import pytest
        print("[OK] pytest установлен")
    except ImportError:
        print("[FAIL] pytest не установлен")
        print("Установите: pip install -r requirements-dev.txt")
        return 1
    
    # Запуск тестов
    tests_passed = run_command(
        ["pytest", "tests/", "-v", "--tb=short"],
        "Запуск всех тестов"
    )
    
    if not tests_passed:
        print("\n[WARN] Некоторые тесты не прошли, но это может быть нормально")
        print("Проверьте вывод выше для деталей")
    
    # Итоговый отчет
    print_header("5. ИТОГОВЫЙ ОТЧЕТ")
    
    checks = [
        ("Новые файлы", all_files_exist),
        ("Импорты", all_imports_ok),
        ("Базовая функциональность", True),
        ("Unit тесты", tests_passed),
    ]
    
    print("Результаты проверок:\n")
    for check_name, passed in checks:
        status = "[OK]" if passed else "[FAIL]"
        print(f"{status} {check_name}")
    
    all_passed = all(passed for _, passed in checks)
    
    if all_passed:
        print("\n" + "=" * 80)
        print("SUCCESS: ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ УСПЕШНО!")
        print("=" * 80)
        print("\nСледующие шаги:")
        print("1. Прочитайте QUICKSTART.md для быстрого старта")
        print("2. Изучите examples.py для примеров использования")
        print("3. Прочитайте MIGRATION.md для интеграции в существующий код")
        print("\nДля запуска агента: python main.py")
        return 0
    else:
        print("\n" + "=" * 80)
        print("WARNING: НЕКОТОРЫЕ ПРОВЕРКИ НЕ ПРОШЛИ")
        print("=" * 80)
        print("\nПроверьте вывод выше для деталей.")
        print("Возможно, нужно установить зависимости:")
        print("  pip install -r requirements.txt")
        print("  pip install -r requirements-dev.txt")
        return 1


if __name__ == "__main__":
    sys.exit(main())
