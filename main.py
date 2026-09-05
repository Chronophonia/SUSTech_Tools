"""
main.py 南科大TIS喵课助手

@CreateDate 2021-1-9
@UpdateDate 2026-9-5
"""

import _thread
import time
import os
from getpass import getpass
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
# 由于Tis的新限制，逻辑改为同时只选一门课


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
        s = input(INFO + "是否保存录入的信息（y/N）？")
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


def submit(semesterData, session, loop=3):
    """ 用于向tis发送喵课的请求
    这里假设主要耗时在网络IO上，本地处理时间几乎可以忽略
    （什么，购物车是怎么回事？那首先排除教务系统是个魔改的电商项目）"""
    for attempt in range(loop):
        if not courseList:
            print(SUCCESS + "所有课程已喵完，再见😾")
            exec("os._exit(0)")  # lint hack
        courseId, courseType, courseName = courseList[0]
        data = {
            "p_pylx": 1,
            "p_xktjz": "rwtjzyx",  # 提交至，可选任务，rwtjzgwc提交至购物车，rwtjzyx提交至已选 gwctjzyx购物车提交至已选
            "p_xn": semesterData['p_xn'],
            "p_xq": semesterData['p_xq'],
            "p_xnxq": semesterData['p_xnxq'],
            "p_xkfsdm": courseType,  # 选课方式(含义见 COURSE_TYPE 注释)
            "p_id": courseId,  # 课程id
            "p_sfxsgwckb": 1,  # 固定
        }
        # addGouwuche 同样受用户级限流，连发会返回“查询请求频率过高”。
        # 若不当处理，课程会一直卡在队首无法前进。这里退避重试，仍失败则放弃本轮。
        req = None
        res = None
        for _ in range(6):  # 限流退避上限
            try:
                req = session.post('https://tis.sustech.edu.cn/Xsxk/addGouwuche', data=data, headers=head, verify=False)
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
            # 6次退避后仍被限流/异常：本轮放弃，课程保留在队首，等用户稍后再按回车触发
            print("[\x1b[0;31m!\x1b[0m] " + f"({courseName})持续被限流或失败，本轮放弃，请等几秒再按回车", flush=True)
            continue
        if "成功" in req.text:
            print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
            print("[\x1b[0;34m█\x1b[0m]\t\t\t" + res, flush=True)
            print("[\x1b[0;34m{}\x1b[0m]".format("=" * 50), flush=True)
            courseList.pop(0)
        else:
            print("[\x1b[0;30m-\x1b[0m]\t\t\t" + res, flush=True)
        if any(map(lambda x: x in req.text, ["冲突", "已选", "已满"])):
            print(f"[\x1b[0;31m!\x1b[0m] ({courseName})因为({res})跳过", flush=True)
            courseList.pop(0)
        time.sleep(1)


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
    # 喵课主逻辑(队列为空即正常结束，不再无限空转)
    while courseList:
        if input(STAR + "按一下回车喵三次，多按同时喵多次，任意字符跳过当前课程\n"):
            courseList.pop(0)
        try:
            _thread.start_new_thread(submit, (semesterInfo, session, 3))
        except Exception as e:
            print(f"[{e}] 线程异常")
    print(SUCCESS + "课程队列已处理完毕")

