import os
import sys
import subprocess
import re

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT_DIR, "data")
LOCAL_SITE_DIR = os.path.join(DATA_DIR, "site-packages")

# 将本地依赖目录优先加入 sys.path，确保能识别已安装的包
if LOCAL_SITE_DIR not in sys.path:
    sys.path.insert(0, LOCAL_SITE_DIR)

def install_dependencies_sequentially():
    requirements_path = os.path.join(ROOT_DIR, "req.txt")
    if not os.path.exists(requirements_path):
        print("未找到 req.txt，跳过依赖安装。")
        return

    os.makedirs(LOCAL_SITE_DIR, exist_ok=True)

    with open(requirements_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        # 跳过空行和注释
        if not line or line.startswith('#'):
            continue
            
        # 提取包的显示名，用于日志输出（例如 aiohttp>=3.9.1 提取出 aiohttp）
        pkg_name = re.split(r'[=><\[]', line)[0].strip()
        
        print(f"正在检查/安装依赖: {line} ...", flush=True)
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",       # 禁用缓存，极大降低内存占用
            "--target",
            LOCAL_SITE_DIR,
            line                    # 逐个传递依赖项
        ]
        try:
            # 隐藏常规的输出以保持整洁，如果出错会抛出异常
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            print(f"✅ {pkg_name} 安装完成或已满足要求")
        except subprocess.CalledProcessError as e:
            print(f"❌ 安装 {line} 失败！")
            # 失败时重新运行一次不隐藏输出的命令，以便暴露具体错误
            subprocess.run(command)
            sys.exit(1)

if __name__ == "__main__":
    print("=== 开始顺序检查并安装依赖 (低内存防 OOM 模式) ===")
    install_dependencies_sequentially()
    print("=== 所有依赖就绪，正在启动主程序 ===")
    
    # 依赖安装完毕，将控制权移交给 main.py
    import main
    main.main()
