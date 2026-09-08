"""
main.py 南科大TIS喵课助手

@CreateDate 2021-1-9
@UpdateDate 2026-9-5
"""

import _thread
import random
import time
import os
from getpass import getpass
from datetime import datetime
from json import loads, dumps
from re import findall

import requests
from colorama import init

import sys
import warnings
from urllib3.exceptions import InsecureRequestWarning


def warn(message, category, filename, lineno, _file=None, line=None):
    if category is not InsecureRequestWarning:
        sys.stderr.write(warnings.formatwarning(message, category, filename, lineno, line))


CLASS_CACHE_PATH = "class.txt"  # 待喵课程列表(手动维护)
COURSE_INFO_PATH = "course.json"  # 抓回的课程信息缓存
warnings.showwarning = warn
SUCCESS = "[\x1b[0;32m+\x1b[0m] "
STAR = "[\x1b[0;32m*\x1b[0m] "
ERROR = "[\x1b[0;31mx\x1b[0m] "
INFO = "[\x1b[0;36m!\x1b[0m] "
FAIL = "[\x1b[0;33m-\x1b[0m] "
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
head = {
    "user-agent": UA,
    "x-requested-with": "XMLHttpRequest"
}

# TIS “选课方式”代码(请求字段 p_xkfsdm 的值,由后端规定、为拼音缩写)。
# 以下字典把这些缩写映射成便于显示的中文类别名,用于打印；不要改动键名。
#   bxxk     = 必修选课(bx=必修, xk=选课) → 通识必修课程
#   xxxk     = 选修选课(xx=选修, xk=选课) → 通识选修课程
#   kzyxk    = 培养方案内课程(本专业培养方案内的课程)
#   zynknjxk = 非培养方案内课程(培养方案之外的课程)
#   jhnxk    = 计划内选课新生(新生计划内选课)
COURSE_TYPE = {
    'bxxk': "通识必修选课",
    'xxxk': "通识选修选课",
    "kzyxk": '培养方案内课程',
    "zynknjxk": '非培养方案内课程',
    "jhnxk": '计划内选课新生',
}

courseList = []  # 需要喵的课程队列
# Tis在用户层面做了限流，早期版本因此被迫"同一时刻只抢一门课"。
# 本版放宽为并发：每按一次回车，同时对队列里最多 MAX_CONCURRENT 门"不同"课程各做几轮爆发式抢课；
# 每门课由独立线程独占(不会并发重复抢同一门)，已有线程在抢的课不会被重复指派。
MAX_CONCURRENT = 2      # 同时并发抢课的课程数上限(最多同时抢3门)
_course_guard = _thread.allocate_lock()  # 保护 courseList / owned_courses
active_workers = 0      # 当前存活的抢课线程数
owned_courses = set()   # 正在被某线程独占抢课的课程 id()


def load_course():
    """ 用于加载本地要喵的课程
    如果存在文件就读文件里的，不存在就手动录入
    有些(我忘了是哪些了)情况会在文件头会有几个不可见字符，但是会被python读进来，所以第一行建议忽略留空"""
    courses = []
    if os.path.exists(CLASS_CACHE_PATH) and os.path.isfile(CLASS_CACHE_PATH):
        print(INFO + "读取规划课表...")
        with open(CLASS_CACHE_PATH, "r", encoding="utf8") as f:
            courses = f.readlines()
        print(SUCCESS + "规划课表读取完毕")
    else:
        print(FAIL + "没有找到规划课表，请手动输入课程信息，输入-1结束录入")
        s = "===本文件是待喵课程的列表，一行输入一个课程名字==请勿删除本行==="
        while s != "-1":
            courses.append(s)
            s = input()
        s = input(INFO + "是否保存录入的信息（y/n）？")
        if s in "yY":
            with open(CLASS_CACHE_PATH, "w", encoding="utf8") as f:
                f.writelines('\n'.join(courses))
    return courses


def cas_login(sid, pwd):
    import re
    print(INFO + "测试CAS链接...")
    login_url = "https://cas.sustech.edu.cn/cas/login?service=https%3A%2F%2Ftis.sustech.edu.cn%2Fcas"
    session = requests.Session()

    # 设置完整的浏览器请求头（模拟真实浏览器）
    session.headers.update({
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-CN,zh;q=0.8,en-US;q=0.5,en;q=0.3',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    })

    try:
        # 1. 获取登录页面（提取所有隐藏字段）
        resp = session.get(login_url, verify=False)
        resp.raise_for_status()
        print(SUCCESS + "成功连接到CAS...")
        html = resp.text

        # 提取 execution（必填）
        execMatch = re.search(r'name="execution"\s+value="([^"]+)"', html)
        if not execMatch:
            print(ERROR + "未找到 execution 字段，登录页面结构可能已变化")
            return None
        execution = execMatch.group(1)

        # 提取 lt（可选）
        ltMatch = re.search(r'name="lt"\s+value="([^"]+)"', html)
        lt = ltMatch.group(1) if ltMatch else ''

        # 提取 service（可选，但通常与 URL 中一致）
        serviceMatch = re.search(r'name="service"\s+value="([^"]+)"', html)
        service = serviceMatch.group(1) if serviceMatch else 'https://tis.sustech.edu.cn/cas'

        # 构建登录数据
        data = {
            'username': sid,
            'password': pwd,
            'execution': execution,
            '_eventId': 'submit',
        }
        if lt:
            data['lt'] = lt
        if service:
            data['service'] = service

        # 打印调试信息（隐藏密码）
        debugData = data.copy()
        debugData['password'] = '******'
        print("[DEBUG] 提交数据:", debugData)

        # 2. 提交登录（添加 Referer 和 Content-Type）
        print(INFO + "登录中...")
        # 更新 headers，添加 Referer 和 Content-Type
        session.headers.update({
            'Referer': login_url,
            'Content-Type': 'application/x-www-form-urlencoded',
        })
        resp = session.post(login_url, data=data, allow_redirects=True, verify=False)

        # 3. 检查是否成功（跳转到 tis）
        if resp.url.startswith("https://tis.sustech.edu.cn"):
            print(SUCCESS + "登录成功")
            return session
        else:
            # 如果未跳转，保存响应以便分析
            with open("login_failed.html", "w", encoding="utf-8") as f:
                f.write(resp.text)
            print(ERROR + "登录失败，响应已保存为 login_failed.html")
            # 尝试从响应中提取错误提示
            errorMatch = re.search(r'<div[^>]*class="errors"[^>]*>(.*?)</div>', resp.text, re.DOTALL)
            if errorMatch:
                print("[DEBUG] 错误信息:", errorMatch.group(1).strip())
            else:
                # 提取页面标题
                titleMatch = re.search(r'<title>(.*?)</title>', resp.text)
                title = titleMatch.group(1) if titleMatch else "无标题"
                print("[DEBUG] 响应页面标题:", title)
            return None

    except Exception as ex:
        print(ERROR + f"CAS登录异常: {ex}")
        return None

def getinfo(semesterData, session):
    """ 用于向tis请求当前学期的课程ID，得到的ID将用于选课的请求
    输入当前学期的日期信息，返回的json包括了课程名和内部的ID """
    if os.path.exists(COURSE_INFO_PATH) and os.path.isfile(COURSE_INFO_PATH):
        print(INFO + f"读取本地缓存 {COURSE_INFO_PATH}，如需重新获取请删除该文件")
        try:
            with open(COURSE_INFO_PATH, "r", encoding="utf8") as f:
                cache = loads(f.read())
            if cache.get('p_xnxq') == semesterData['p_xnxq']:
                courseInfo = cache.get('courses') or {}
                print(SUCCESS + f"课程信息读取完毕，共读取{str(len(courseInfo))}门课程信息\n")
                return courseInfo
            else:
                print(INFO + "缓存文件已过期，重新获取课程信息")
        except Exception as ex:
            print(ERROR + f"缓存文件损坏，重新获取课程信息，{ex}")
    print(INFO + "从服务器下载课程信息，请稍等...")
    courseInfo = {}
    for courseType in COURSE_TYPE.keys():
        data = {
            "p_xn": semesterData['p_xn'],  # 当前学年
            "p_xq": semesterData['p_xq'],  # 当前学期
            "p_xnxq": semesterData['p_xnxq'],  # 当前学年学期
            "p_pylx": 1,
            "mxpylx": 1,
            "p_xkfsdm": courseType,  # 选课方式(含义见上方 COURSE_TYPE 注释)
            "pageNum": 1,
            "pageSize": 1000  # 每学期总共开课在1000左右，所以单分类可以包括学期的全部课程
        }
        # TIS做了用户级限流：多个查询连发时，后发的会被限流返回空(频率过高)，
        # 导致漏抓某些类型(比如只抓到bxxk)。因此每个类型要退避重试，类型之间留间隔。
        for fetchTry in range(5):
            print("[\x1b[0;36m*\x1b[0m] " + f"获取 {COURSE_TYPE[courseType]} 列表(第{fetchTry + 1}/5次尝试)...")
            try:
                req = session.post('https://tis.sustech.edu.cn/Xsxk/queryKxrw', data=data, headers=head, verify=False)
                rawClassData = loads(req.text)
            except Exception as ex:
                rawClassData = None
                print(ERROR + f"{COURSE_TYPE[courseType]} 请求异常：{ex}")
            if isinstance(rawClassData, dict) and 'kxrwList' in rawClassData:
                subList = (rawClassData['kxrwList'] or {}).get('list') or []
                for row in subList:
                    courseInfo[row['rwmc']] = (row['id'], courseType)
                print("[\x1b[0;32m*\x1b[0m] " + f"{COURSE_TYPE[courseType]} 返回 {len(subList)} 门")
                break
            msg = ''
            if isinstance(rawClassData, dict):
                msg = rawClassData.get('message') or ''
            wait = 2 * (fetchTry + 1)
            print(FAIL + f"{COURSE_TYPE[courseType]} 请求被限流或返回异常{('：' + msg) if msg else ''}，{wait}s后重试")
            time.sleep(wait)
        else:
            print(FAIL + f"{COURSE_TYPE[courseType]} 多次尝试仍失败，本类型课程可能缺失。建议稍后删除 {COURSE_INFO_PATH} 重新获取")
        time.sleep(1)  # 类型之间留间隔，避免触发限流
    print(SUCCESS + f"课程信息读取完毕，共读取{str(len(courseInfo))}门课程信息")
    choice = input(INFO + "是否保存读取的课程信息（y/n）？")
    if choice in "yY":
        with open(COURSE_INFO_PATH, "w", encoding="utf8", newline="\n") as f:
            # 结构：{"p_xnxq": 学期, "courses": {课程名: [id, 选课类型]}}
            f.write(dumps({"p_xnxq": semesterData['p_xnxq'], "courses": courseInfo},
                          ensure_ascii=False, indent=1))
    return courseInfo


def claim_next_course():
    """ 从队首取出一门"当前没被并发线程占用"的课并登记占用，返回给调用线程独占；
    没有可取(队列空或全被占用)则返回 None。调用方结束时必须 pop_course / release_course。"""
    with _course_guard:
        for item in courseList:
            if id(item) not in owned_courses:
                owned_courses.add(id(item))
                return item
    return None


def pop_course(item):
    """ 该课程已有定论(成功/冲突/已满/被跳过)：从队列移除并释放占用 """
    with _course_guard:
        for idx, course in enumerate(courseList):
            if course is item:
                courseList.pop(idx)
                break
        owned_courses.discard(id(item))


def release_course(item):
    """ 本轮爆发结束但课程尚无定论：只释放占用，课程保留在队首等待下次回车重试 """
    with _course_guard:
        owned_courses.discard(id(item))


def submit(semesterData, session, item, loop=3):
    """ 用于向tis发送喵课请求，只针对调用方独占的这一门课(item)做"爆发式"抢课
    （这里假设主要耗时在网络IO上，本地处理时间几乎可以忽略）。
    最多连续尝试 loop 轮，命中 成功/冲突/已满 即移除课程并结束；
    命中限流则退避重试；其它原因(如未开放)不消耗课程，本轮结束保留队列，等下次回车重试。"""
    courseId, courseType, courseName = item
    for _ in range(loop):
        with _course_guard:
            if not any(course is item for course in courseList):
                return  # 这门课已被手动跳过或其它线程移除，别再空打
        # addGouwuche 同样受用户级限流，连发会返回"查询请求频率过高"。退避重试，仍失败则放弃本轮。
        req = None
        res = None
        for _ in range(6):  # 限流退避上限
            try:
                req = session.post('https://tis.sustech.edu.cn/Xsxk/addGouwuche', data={
                    "p_pylx": 1,
                    "p_xktjz": "rwtjzyx",  # 提交至可选任务(rwtjzyx)；rwtjzgwc=提交至购物车；gwctjzyx=购物车提交至已选
                    "p_xn": semesterData['p_xn'],
                    "p_xq": semesterData['p_xq'],
                    "p_xnxq": semesterData['p_xnxq'],
                    "p_xkfsdm": courseType,  # 选课方式(含义见 COURSE_TYPE 注释)
                    "p_id": courseId,  # 课程id
                    "p_sfxsgwckb": 1,  # 固定
                }, headers=head, verify=False)
                res = loads(req.text)['message']
            except Exception as ex:
                req = None
                res = str(ex)
                print("[\x1b[0;31m!\x1b[0m] " + f"({courseName})请求异常：{ex}", flush=True)
                time.sleep(2)
                continue
            if not any(k in req.text for k in ("频率过高", "请稍后")):
                break
            print("[\x1b[0;30m-\x1b[0m]\t\t\t" + res, flush=True)
            time.sleep(2)
        else:
            # 6次退避后仍被限流/异常：放弃本轮，课程保留在队首，等用户稍后再按回车触发
            print("[\x1b[0;31m!\x1b[0m] " + f"({courseName})持续被限流或失败，本轮放弃，请等几秒再按回车", flush=True)
            return
        if "成功" in req.text:
            print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
            print("[\x1b[0;34m█\x1b[0m]\t\t\t" + res, flush=True)
            print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
            pop_course(item)
            return
        if any(map(lambda x: x in req.text, ["冲突", "已选", "已满"])):
            print(f"[\x1b[0;31m!\x1b[0m] ({courseName})因为({res})跳过", flush=True)
            pop_course(item)
            return
        print("[\x1b[0;30m-\x1b[0m]\t\t\t" + res, flush=True)
        time.sleep(1)


def _grab_worker(semesterData, session, item, delay=0.1):
    """ 一个并发抢课线程：独占 item 这一门课，做几轮爆发式抢课，结束后释放占用的名额 """
    global active_workers
    try:
        if delay > 0:
            time.sleep(delay)  # 一批线程同时开火时错峰，避免同时命中限流
        submit(semesterData, session, item)
    finally:
        release_course(item)
        with _course_guard:
            active_workers -= 1


def top_up_workers(semesterData, session):
    """ 每次回车/每轮自动补线程：把并发抢课线程补齐到不超过 MAX_CONCURRENT 个，
    每个线程独占队列里一门不同的课；已有线程还在抢的课不会被重复指派。
    同一批补位中：第一门(队首=最优先)立即开火(delay=0，保证开抢瞬间第一枪准点)，
    其余课程小幅错峰(delay≈0.1~0.3s)，避免几门同时打到服务器触发限流。 """
    global active_workers
    wave = 0
    while True:
        with _course_guard:
            if active_workers >= MAX_CONCURRENT:
                return
        item = claim_next_course()
        if item is None:
            return
        with _course_guard:
            active_workers += 1
        delay = 0.0 if wave == 0 else random.uniform(0.1, 0.3)
        wave += 1
        try:
            _thread.start_new_thread(_grab_worker, (semesterData, session, item, delay))
        except Exception as ex:
            print(f"[\x1b[0;31m!\x1b[0m] 启动线程失败：{ex}", flush=True)
            with _course_guard:
                active_workers -= 1
                owned_courses.discard(id(item))


def parse_open_time(s):
    """ 解析用户输入的开抢时刻：支持 "12:30" / "12:30:00" / "2026-09-06 12:30:00"。
    只给时间则视为今天；解析失败返回 None。"""
    s = (s or '').strip()
    if not s:
        return None
    now = datetime.now()
    parsed = None
    matched = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(s, fmt)
            matched = fmt
            break
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    if matched in ("%H:%M:%S", "%H:%M"):  # 只给了时间、没给日期：补成今天
        parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
    return parsed


def wait_until_open(openAt, session):
    """ 到开抢时刻前倒计时等待：打印剩余秒数，并每60秒给会话保活(防止等太久登录态过期)。
    到达时刻返回 True；等待中被 Ctrl+C 取消返回 False(由调用方决定后续)。
    倒计时的显示刷新率与“准点”无关，真正的准点由循环轮询间隔决定：
    剩余>2秒时 0.1s 一次足够；最后2秒收紧到 20ms 轮询，把开抢误差压到 ~20ms 内。"""
    lastBeat = time.time()
    lastShown = None
    print(INFO + "已进入倒计时等待，每60秒自动保活会话；等待中按 Ctrl+C 可转为手动回车模式")
    try:
        while True:
            remain = (openAt - datetime.now()).total_seconds()
            if remain <= 0:
                print("\n" + SUCCESS + "开抢时刻到！")
                return True
            if time.time() - lastBeat >= 60:
                try:
                    session.post('https://tis.sustech.edu.cn/Xsxk/queryXkdqXnxq',
                                 data={'mxpylx': 1}, verify=False)
                    lastBeat = time.time()
                except Exception:
                    print(FAIL + "会话保活请求失败，可能已掉线，请留意", flush=True)
            shown = round(remain, 1)
            if shown != lastShown:  # 数值变化才刷一行，避免高刷新率时疯狂刷屏
                print(f"\r[倒计时] 距开抢还有 {remain:6.1f} 秒 ...", end='', flush=True)
                lastShown = shown
            time.sleep(0.1 if remain > 2 else 0.02)
    except KeyboardInterrupt:
        print("\n" + INFO + "已取消自动等待")
        return False


def auto_grab(semesterData, session):
    """ 无人值守模式：到点后持续把并发线程补满(最多 MAX_CONCURRENT 门)，直到队列清空。
    无需人在场按回车；Ctrl+C 可停止。"""
    print(INFO + f"自动抢课中：持续并发抢当前队列(最多{MAX_CONCURRENT}门)，队列清空即结束，Ctrl+C 可停止")
    while courseList:
        top_up_workers(semesterData, session)
        time.sleep(0.5)
    while active_workers:  # 等最后一轮并发线程收尾
        time.sleep(0.2)
    print(SUCCESS + "课程队列已处理完毕")


def manual_grab(semesterData, session):
    """ 手动回车模式：每按一次回车，把并发线程补齐到最多 MAX_CONCURRENT 门各抢一轮。"""
    print(INFO + f"每按一次回车，会同时对当前队列里最多 {MAX_CONCURRENT} 门不同的课程各抢几轮；"
                 f"已有线程在抢的课不会被重复抢，并发达到 {MAX_CONCURRENT} 门时多余回车会自动等待空位。")
    while courseList:
        line = input(STAR + f"按回车抢下一轮（同时最多{MAX_CONCURRENT}门），输入任意字符再回车可跳过当前这门课\n")
        if line:
            with _course_guard:
                if courseList:
                    courseList.pop(0)
        top_up_workers(semesterData, session)
    while active_workers:  # 等最后一轮并发线程收尾
        time.sleep(0.2)
    print(SUCCESS + "课程队列已处理完毕")


if __name__ == '__main__':
    init(autoreset=True)  # 某窗口系统的优质终端并不直接支持如下转义彩色字符，所以需要一些库来帮忙
    courseNameList = load_course()  # 读取本地待喵的课程
    # 下面是CAS登录
    session = None
    while session is None:
        userName = input("请输入您的学号：")
        passWord = input("请输入CAS密码（密码不显示，输入完按回车即可）：")
        session = cas_login(userName, passWord)
        if session is None:
            print(FAIL + "请重试...")

    # 不再手动设置 head['cookie']，后续所有请求使用 session
    # 但 head 仍然保留用于 User-Agent 等，但不再包含 cookie

    semesterInfo = loads(
        session.post('https://tis.sustech.edu.cn/Xsxk/queryXkdqXnxq',
                     data={'mxpylx': 1}, verify=False).text
    )
    print(SUCCESS + f"当前学期是{semesterInfo['p_xn']}学年第{semesterInfo['p_xq']}学期，为"
                    f"{['', '秋季', '春季', '小'][int(semesterInfo['p_xq'])]}学期")
    # 然后获取本学期全部课程信息
    print(INFO + "读取课程信息...")
    courseInfo = getinfo(semesterInfo, session)
    # 分析要喵课程的ID
    for courseName in courseNameList:
        courseName = courseName.strip()
        if courseName in courseInfo:
            courseId, courseType = courseInfo[courseName]
            courseList.append([courseId, courseType, courseName])
    print("[\x1b[0;34m{}\x1b[0m]".format("=" * 25))
    for queuedCourse in courseList:
        print(f"{COURSE_TYPE[queuedCourse[1]]} : {queuedCourse[2]}\t\tID为: {queuedCourse[0]}")
    print("[\x1b[0;34m{}\x1b[0m]".format("=" * 25))
    print(SUCCESS + "成功读入以上信息\n")
    # 未匹配上的课程单独提示，避免以为进队了却没进
    for courseName in courseNameList:
        courseName = courseName.strip()
        if courseName and courseName not in courseInfo:
            print(ERROR + f"未在可选课程中找到：{courseName}")
    if not courseList:
        print(ERROR + "没有课程能加入选课队列，为避免死循环直接退出")
        print(FAIL + "请检查 class.txt 中的课程名是否与本学期开课名完全一致")
        print(FAIL + f"提示：getinfo 被限流时可能漏抓某些类型(只抓到bxxk等)。删除 {COURSE_INFO_PATH} 后重跑即可全量重新抓取")
        raise SystemExit(1)
    # ===== 喵课主逻辑 =====
    # 时间优化：系统在固定时刻开放。填开抢时刻后，脚本会等在这个时刻自动开抢(无人值守)，
    # 在开放瞬间自动发出第一批请求，避免人手卡点慢半拍；直接回车则退回手动回车模式。
    openAt = None
    raw = input(INFO + "本次开抢时刻（如 12:30:00 或 2026-09-06 12:30:00，直接回车=手动回车模式）：").strip()
    if raw:
        openAt = parse_open_time(raw)
    if openAt is not None and openAt <= datetime.now():
        print(FAIL + "填写的时刻已过，将立即开抢（如需明天请填完整日期时间）")
        openAt = None  # 标记立即开抢：走 target=None + auto 分支
        autoNow = True
    else:
        autoNow = False
    if raw and openAt is None and not autoNow:
        print(FAIL + f"无法解析时间「{raw}」，已退回手动回车模式")
    try:
        if autoNow:
            auto_grab(semesterInfo, session)
        elif openAt is not None:
            if wait_until_open(openAt, session):
                auto_grab(semesterInfo, session)
            else:
                manual_grab(semesterInfo, session)  # 等待中被取消 → 转手动
        else:
            manual_grab(semesterInfo, session)
    except KeyboardInterrupt:
        print("\n" + ERROR + "已退出")

