import re
from collections import Counter
from datetime import datetime, timezone, timedelta

import matplotlib.pyplot as plt
import pandas as pd
import plotly.express as px
import streamlit as st
import googleapiclient.discovery
import googleapiclient.errors
from wordcloud import WordCloud

st.set_page_config(page_title="유튜브 채널 분석기", page_icon="📺", layout="wide")

KST = timezone(timedelta(hours=9))
FONT_PATH = "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"  # packages.txt로 설치됨
MAIN_COLOR = "#E5484D"
SUB_COLOR = "#3E63DD"

STOPWORDS = {
    "그리고", "그런데", "하지만", "그래서", "진짜", "정말", "너무", "이거", "저거", "그거",
    "있는", "없는", "하는", "같은", "영상", "보고", "보면", "합니다", "입니다", "shorts",
    "Shorts", "SHORTS", "the", "and", "for", "with",
}


# ---------------------------------------------------------
# 1. API 키 (코드에 직접 쓰지 않고 Streamlit Secrets에서 불러옴)
# ---------------------------------------------------------
try:
    API_KEY = st.secrets["YOUTUBE_API_KEY"]
except Exception:
    st.error("API 키가 없습니다. Streamlit Secrets에 YOUTUBE_API_KEY를 등록해 주세요.")
    st.stop()


@st.cache_resource
def get_youtube():
    return googleapiclient.discovery.build("youtube", "v3", developerKey=API_KEY)


youtube = get_youtube()


# ---------------------------------------------------------
# 2. 입력 해석 도우미
# ---------------------------------------------------------
def parse_channel_input(text: str):
    """채널 링크/핸들/ID를 ('handle'|'id', 값) 형태로 변환"""
    s = text.strip()
    m = re.search(r"/channel/(UC[\w-]{22})", s)
    if m:
        return "id", m.group(1)
    if re.fullmatch(r"UC[\w-]{22}", s):
        return "id", s
    m = re.search(r"@([\w.\-가-힣]+)", s)
    if m:
        return "handle", m.group(1)
    if s and "/" not in s:
        return "handle", s
    return None, None


def parse_duration(iso: str) -> int:
    """ISO 8601 길이(PT1H2M3S)를 초 단위로"""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


# ---------------------------------------------------------
# 3. 데이터 수집 (10분 캐시 → 같은 채널 다시 볼 때 할당량 절약)
# ---------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def fetch_channel(kind: str, value: str, max_videos: int):
    params = {"part": "snippet,statistics,contentDetails"}
    params["forHandle" if kind == "handle" else "id"] = value
    ch = youtube.channels().list(**params).execute()
    if not ch.get("items"):
        return None, []
    item = ch["items"][0]
    info = {
        "title": item["snippet"]["title"],
        "description": item["snippet"].get("description", ""),
        "thumbnail": item["snippet"]["thumbnails"].get("high", item["snippet"]["thumbnails"]["default"])["url"],
        "created": item["snippet"]["publishedAt"][:10],
        "subscribers": int(item["statistics"].get("subscriberCount", 0)),
        "hidden_subs": item["statistics"].get("hiddenSubscriberCount", False),
        "total_views": int(item["statistics"].get("viewCount", 0)),
        "video_count": int(item["statistics"].get("videoCount", 0)),
    }
    uploads = item["contentDetails"]["relatedPlaylists"]["uploads"]

    video_ids, token = [], None
    while len(video_ids) < max_videos:
        pl = youtube.playlistItems().list(
            part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token
        ).execute()
        video_ids += [i["contentDetails"]["videoId"] for i in pl.get("items", [])]
        token = pl.get("nextPageToken")
        if not token:
            break
    video_ids = video_ids[:max_videos]

    raw = []
    for i in range(0, len(video_ids), 50):
        vr = youtube.videos().list(
            part="snippet,statistics,contentDetails", id=",".join(video_ids[i:i + 50])
        ).execute()
        raw += vr.get("items", [])
    return info, raw


def build_dataframe(raw):
    now = datetime.now(timezone.utc)
    rows = []
    for v in raw:
        published = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
        st_ = v.get("statistics", {})
        views = int(st_.get("viewCount", 0))
        likes = int(st_.get("likeCount", 0))
        comments = int(st_.get("commentCount", 0))
        seconds = parse_duration(v.get("contentDetails", {}).get("duration", ""))
        days = max((now - published).total_seconds() / 86400, 1 / 24)
        local = published.astimezone(KST)
        rows.append({
            "제목": v["snippet"]["title"],
            "게시일시": local,
            "게시일": local.date(),
            "월": local.strftime("%Y-%m"),
            "요일": "월화수목금토일"[local.weekday()],
            "시간대": local.hour,
            "길이(초)": seconds,
            "형식": "쇼츠" if 0 < seconds <= 180 else "일반 영상",
            "조회수": views,
            "좋아요": likes,
            "댓글수": comments,
            "참여율(%)": round((likes + comments) / views * 100, 2) if views else 0,
            "하루당 조회수": round(views / days, 1),
            "제목 길이": len(v["snippet"]["title"]),
            "링크": f"https://youtu.be/{v['id']}",
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("게시일시").reset_index(drop=True)
    return df


def title_wordcloud(titles):
    words = []
    for t in titles:
        t = re.sub(r"#\S+|http\S+|\[.*?\]", " ", t)
        words += [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", t) if w not in STOPWORDS]
    counts = Counter(words)
    if not counts:
        return None, counts
    wc = WordCloud(
        font_path=FONT_PATH, width=1000, height=500, background_color="white",
        max_words=80, colormap="Reds", prefer_horizontal=0.9,
    ).generate_from_frequencies(counts)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.imshow(wc, interpolation="bilinear")
    ax.axis("off")
    fig.tight_layout(pad=0)
    return fig, counts


def fmt(n: int) -> str:
    if n >= 100_000_000:
        return f"{n / 100_000_000:.1f}억"
    if n >= 10_000:
        return f"{n / 10_000:.1f}만"
    return f"{n:,}"


def style(fig, height=380):
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=40, b=10),
        font=dict(family="sans-serif"), legend_title_text="",
    )
    return fig


# ---------------------------------------------------------
# 4. 화면
# ---------------------------------------------------------
st.title("📺 유튜브 채널 분석기")
st.caption("채널 링크를 넣으면 최근 영상들을 모아 조회수·업로드 패턴·인기 키워드를 시각화합니다.")

with st.form("input"):
    c1, c2 = st.columns([3, 1])
    channel_input = c1.text_input(
        "채널 링크 또는 @핸들", placeholder="https://www.youtube.com/@Unrealscience"
    )
    max_videos = c2.selectbox("분석할 최근 영상 수", [50, 100, 200, 300, 500], index=1)
    submitted = st.form_submit_button("분석하기", type="primary", use_container_width=True)

if submitted:
    st.session_state["target"] = (channel_input, max_videos)

if "target" not in st.session_state:
    st.info("위에 채널 링크를 넣고 **분석하기**를 눌러 주세요.")
    st.stop()

channel_input, max_videos = st.session_state["target"]
kind, value = parse_channel_input(channel_input)
if not kind:
    st.warning("채널 링크 형식을 확인해 주세요. 예: https://www.youtube.com/@Unrealscience")
    st.stop()

try:
    with st.spinner("채널 데이터를 불러오는 중..."):
        info, raw = fetch_channel(kind, value, max_videos)
except googleapiclient.errors.HttpError as e:
    st.error(f"API 오류: {e}")
    st.stop()

if info is None:
    st.warning("채널을 찾을 수 없습니다. 링크나 핸들을 다시 확인해 주세요.")
    st.stop()

df = build_dataframe(raw)
if df.empty:
    st.info("이 채널에는 공개된 영상이 없습니다.")
    st.stop()

# ---------- 채널 개요 ----------
h1, h2 = st.columns([1, 6])
h1.image(info["thumbnail"], width=110)
h2.subheader(info["title"])
h2.caption(f"개설일 {info['created']} · 최근 영상 {len(df)}개 분석")

m1, m2, m3, m4 = st.columns(4)
m1.metric("구독자", "비공개" if info["hidden_subs"] else fmt(info["subscribers"]))
m2.metric("총 조회수", fmt(info["total_views"]))
m3.metric("전체 영상 수", f"{info['video_count']:,}")
m4.metric("분석 영상 평균 조회수", fmt(int(df["조회수"].mean())))

link_cfg = {"링크": st.column_config.LinkColumn("링크", display_text="보기")}

tab1, tab2, tab3, tab4 = st.tabs(["📈 조회수 흐름", "🏆 인기 영상", "🗓️ 업로드 패턴", "☁️ 키워드"])

# ---------- 탭 1: 조회수 흐름 ----------
with tab1:
    fig = px.scatter(
        df, x="게시일시", y="조회수", color="형식", size="좋아요", size_max=28,
        hover_name="제목", hover_data={"조회수": ":,", "좋아요": ":,", "게시일시": "|%Y-%m-%d"},
        color_discrete_map={"일반 영상": MAIN_COLOR, "쇼츠": SUB_COLOR},
        title="영상별 조회수 (점 크기 = 좋아요 수)",
    )
    fig.add_hline(y=df["조회수"].median(), line_dash="dot", line_color="gray",
                  annotation_text="중앙값", annotation_position="top left")
    st.plotly_chart(style(fig, 450), use_container_width=True)

    monthly = df.groupby("월").agg(영상수=("제목", "count"), 평균조회수=("조회수", "mean")).reset_index()
    c1, c2 = st.columns(2)
    fig = px.bar(monthly, x="월", y="영상수", title="월별 업로드 수",
                 color_discrete_sequence=[SUB_COLOR])
    c1.plotly_chart(style(fig), use_container_width=True)
    fig = px.line(monthly, x="월", y="평균조회수", markers=True, title="월별 평균 조회수",
                  color_discrete_sequence=[MAIN_COLOR])
    c2.plotly_chart(style(fig), use_container_width=True)

# ---------- 탭 2: 인기 영상 ----------
with tab2:
    top = df.nlargest(10, "조회수").sort_values("조회수")
    top["짧은 제목"] = top["제목"].str.slice(0, 28) + top["제목"].str.len().gt(28).map({True: "…", False: ""})
    fig = px.bar(top, x="조회수", y="짧은 제목", orientation="h", hover_name="제목",
                 title="조회수 TOP 10", color_discrete_sequence=[MAIN_COLOR])
    fig.update_yaxes(title="")
    st.plotly_chart(style(fig, 460), use_container_width=True)

    c1, c2 = st.columns(2)
    fig = px.scatter(df, x="조회수", y="참여율(%)", color="형식", hover_name="제목",
                     log_x=True, title="조회수 vs 참여율 (좋아요+댓글 ÷ 조회수)",
                     color_discrete_map={"일반 영상": MAIN_COLOR, "쇼츠": SUB_COLOR})
    c1.plotly_chart(style(fig), use_container_width=True)

    fmt_stats = df.groupby("형식").agg(영상수=("제목", "count"), 평균조회수=("조회수", "mean")).reset_index()
    fig = px.bar(fmt_stats, x="형식", y="평균조회수", text="영상수", color="형식",
                 title="형식별 평균 조회수 (막대 위 숫자 = 영상 수)",
                 color_discrete_map={"일반 영상": MAIN_COLOR, "쇼츠": SUB_COLOR})
    fig.update_traces(texttemplate="%{text}개", textposition="outside")
    fig.update_layout(showlegend=False)
    c2.plotly_chart(style(fig), use_container_width=True)

    st.markdown("**🔥 조회수 높은 영상**")
    cols = ["제목", "게시일", "조회수", "좋아요", "댓글수", "참여율(%)", "링크"]
    st.dataframe(df.nlargest(10, "조회수")[cols], hide_index=True,
                 use_container_width=True, column_config=link_cfg)

    st.markdown("**🐢 게시 기간 대비 조회수 낮은 영상** (하루당 조회수 기준)")
    st.dataframe(df.nsmallest(10, "하루당 조회수")[["제목", "게시일", "조회수", "하루당 조회수", "링크"]],
                 hide_index=True, use_container_width=True, column_config=link_cfg)

# ---------- 탭 3: 업로드 패턴 ----------
with tab3:
    days = list("월화수목금토일")
    heat = (df.groupby(["요일", "시간대"]).size()
              .unstack(fill_value=0)
              .reindex(index=days, columns=range(24), fill_value=0))
    fig = px.imshow(heat, color_continuous_scale="Reds", aspect="auto",
                    labels=dict(x="시간 (한국시간)", y="요일", color="영상 수"),
                    title="언제 영상을 올렸을까? (요일 × 시간대)")
    st.plotly_chart(style(fig, 360), use_container_width=True)

    c1, c2 = st.columns(2)
    by_day = df.groupby("요일")["조회수"].mean().reindex(days).reset_index()
    fig = px.bar(by_day, x="요일", y="조회수", title="요일별 평균 조회수",
                 color_discrete_sequence=[MAIN_COLOR])
    c1.plotly_chart(style(fig), use_container_width=True)

    df["길이(분)"] = (df["길이(초)"] / 60).round(1)
    fig = px.scatter(df, x="길이(분)", y="조회수", color="형식", hover_name="제목",
                     log_y=True, title="영상 길이 vs 조회수",
                     color_discrete_map={"일반 영상": MAIN_COLOR, "쇼츠": SUB_COLOR})
    c2.plotly_chart(style(fig), use_container_width=True)

    gaps = df["게시일시"].diff().dt.total_seconds().dropna() / 86400
    if len(gaps):
        st.info(f"평균 **{gaps.mean():.1f}일**에 한 번 영상을 올렸어요. "
                f"가장 많이 올린 요일은 **{df['요일'].value_counts().idxmax()}요일**, "
                f"가장 많이 올린 시간대는 **{df['시간대'].value_counts().idxmax()}시**입니다.")

# ---------- 탭 4: 키워드 ----------
with tab4:
    st.caption("영상 제목에 자주 나온 단어 (태그·괄호 안 문구 제외)")
    fig, counts = title_wordcloud(df["제목"].tolist())
    if fig:
        st.pyplot(fig)
        top_words = pd.DataFrame(counts.most_common(15), columns=["단어", "횟수"])

        # 단어가 들어간 영상의 평균 조회수
        top_words["포함 영상 평균 조회수"] = [
            int(df[df["제목"].str.contains(re.escape(w))]["조회수"].mean()) for w in top_words["단어"]
        ]
        fig = px.bar(top_words.sort_values("횟수"), x="횟수", y="단어", orientation="h",
                     color="포함 영상 평균 조회수", color_continuous_scale="Reds",
                     title="자주 쓴 단어 TOP 15 (색이 진할수록 그 단어가 들어간 영상 조회수가 높음)")
        st.plotly_chart(style(fig, 480), use_container_width=True)
    else:
        st.info("분석할 단어가 없습니다.")

st.download_button(
    "📥 분석 데이터 CSV로 저장",
    df.drop(columns=["게시일시"]).to_csv(index=False).encode("utf-8-sig"),
    file_name=f"{info['title']}_분석.csv", mime="text/csv",
)
