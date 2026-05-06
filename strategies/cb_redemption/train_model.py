"""
可转债强赎博弈策略 — 模型训练管线

功能：
1. 构建事件切片面板（12维特征）
2. 训练二元 Logit 分类模型
3. OOS 时间序列回测
4. 模型持久化保存

特征集（12维）：
  [1]  close/cb_value          : 转债价格/纯债价值比（债底安全垫）
  [2]  cb_over_rate            : 转股溢价率（关键指标）
  [3]  pct_chg_20d             : 正股20日涨幅
  [4]  pct_chg_60d             : 正股60日涨幅  
  [5]  vol_ma5                 : 转债5日均量比
  [6]  remain_size / issue_size: 剩余规模比（转股进度）
  [7]  shd_ratio_pct           : 原股东配售比例
  [8]  top1_hold_ratio         : 正股第一大股东持股比
  [9]  hold_ratio * shd_ratio  : 大股东持债比例估算（交互项）
  [10] days_from_conv_start    : 距转股起始日天数
  [11] coupon_rate             : 票面利率
  [12] days_to_maturity        : 距到期天数
"""

import os
import pandas as pd
import numpy as np
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")

# ===========================================================================
# 数据加载
# ===========================================================================

def load_all_data():
    """加载全部所需数据"""
    warehouse = '/home/jay/projects/quant/data/cb_warehouse'
    
    daily = pd.read_parquet(f'{warehouse}/cb_daily.parquet')
    basic = pd.read_parquet(f'{warehouse}/cb_basic.parquet')
    call = pd.read_parquet(f'{warehouse}/cb_call.parquet')
    
    # 拉取 cb_issue + cb_top1_holder_est（如果已存在）
    import tushare as ts
    API_TOKEN = os.environ['TUSHARE_TOKEN']
    API_URL = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')
    ts.set_token(API_TOKEN)
    pro = ts.pro_api()
    pro._DataApi__http_url = API_URL
    
    df_issue = pro.cb_issue()
    
    # 尝试加载 top1_holder 数据
    try:
        top1 = pd.read_parquet(f'{warehouse}/cb_top1_holder_est.parquet')
        print(f"已加载 top1_holder_est: {len(top1)} 行")
    except:
        print("top1_holder_est 未找到，尝试加载 top1_holders 并合并...")
        try:
            top1_raw = pd.read_parquet(f'{warehouse}/top1_holders.parquet')
            print(f"已加载 top1_holders: {len(top1_raw)} 行")
            
            # 合并到stk_codes
            m = df_issue.merge(basic[['ts_code','stk_code','issue_size','remain_size']], 
                               on='ts_code', how='inner', suffixes=('_issue', '_basic'))
            m['shd_ratio_pct'] = m['shd_ration_size'] / (m['issue_size_basic'] * 1e8) * 100
            stk_codes = m[['ts_code','stk_code','shd_ratio_pct','remain_size','issue_size_basic']].dropna(subset=['stk_code']).copy()
            stk_codes['stk_code_int'] = stk_codes['stk_code'].apply(lambda x: int(x))
            stk_codes['stk_full'] = stk_codes['stk_code_int'].apply(
                lambda x: str(x)[0:6] + ('.SH' if str(x).startswith('6') else '.SZ'))
            
            top1 = stk_codes.merge(top1_raw, on='stk_full', how='left')
            top1['est_major_holder_cb_ratio'] = top1['shd_ratio_pct'] * top1['top1_hold_ratio'] / 100
            top1['major_holder_cb_est'] = top1['est_major_holder_cb_ratio'].clip(0, 100)
            top1.to_parquet(f'{warehouse}/cb_top1_holder_est.parquet')
        except Exception as e:
            print(f"top1数据加载失败: {e}")
            top1 = None
    
    return daily, basic, call, df_issue, top1


def build_event_panel(daily, basic, call, df_issue, top1):
    """构建事件切片面板"""
    
    print("构建事件切片面板...")
    
    # 1. 筛选call事件
    events = call[call['is_call'].isin(['公告不强赎', '公告实施强赎'])].copy()
    events = events.rename(columns={'ann_date': 'event_date'})
    events['event_dt'] = pd.to_datetime(events['event_date'])
    
    # 2. 合并 cb_issue 的配售比例
    df_issue['shd_ratio_pct'] = df_issue['shd_ration_size'] / (df_issue['issue_size'] * 1e8) * 100
    events = events.merge(df_issue[['ts_code','shd_ratio_pct']], on='ts_code', how='left')
    
    # 3. 合并 top1 数据
    if top1 is not None:
        events = events.merge(
            top1[['ts_code','top1_hold_ratio','top1_holder_name','top1_holder_type',
                  'major_holder_cb_est','remain_size','issue_size_basic']], 
            on='ts_code', how='left')
    
    # 4. 合并 cb_basic 的静态特征
    events = events.merge(
        basic[['ts_code','issue_size','remain_size','stk_code','conv_start_date',
               'maturity_date','coupon_rate','list_date']], 
        on='ts_code', how='left', suffixes=('_event', '_basic'))
    
    # 补齐 remain_size（event表和basic表里都可能有）
    events['remain_size'] = events['remain_size_event'].fillna(events['remain_size_basic'])
    
    # 5. 计算时序特征 - 从cb_daily取事件日前N天的平均值
    daily = daily.sort_values(['ts_code', 'trade_date'])
    daily['dt'] = pd.to_datetime(daily['trade_date'])
    
    feature_rows = []
    total = len(events)
    
    for idx, row in events.iterrows():
        if idx % 200 == 0:
            print(f"  特征计算: {idx}/{total}")
        
        code = row['ts_code']
        evt_dt = row['event_dt']
        
        # 取事件日前60天的daily数据
        window_start = evt_dt - timedelta(days=90)
        hist = daily[(daily['ts_code'] == code) & (daily['dt'] >= window_start) & (daily['dt'] <= evt_dt)].copy()
        
        if len(hist) == 0:
            continue
        
        # 转股溢价率 - 30日均值
        recent = hist.tail(30)
        cb_over_rate_30d = recent['cb_over_rate'].mean() if len(recent) > 0 else np.nan
        
        # 转债价格/纯债价值比 - 30日均值
        close_cb_value_30d = (recent['close'] / recent['cb_value']).mean() if len(recent) > 0 else np.nan
        
        # 正股涨跌幅 - 无法直接从cb_daily拿到，用pct_chg近似
        pct_chg_20d = hist.tail(20)['pct_chg'].sum() if len(hist) >= 20 else np.nan
        pct_chg_60d = hist.tail(60)['pct_chg'].sum() if len(hist) >= 60 else np.nan
        
        # 5日均量比
        vol_ma5 = recent['vol'].mean() if len(recent) > 0 else np.nan
        
        # 剩余规模比
        if row['issue_size'] > 0:
            remain_ratio = row['remain_size'] / row['issue_size']
        else:
            remain_ratio = np.nan
        
        # 距转股起始日天数
        if pd.notna(row.get('conv_start_date')):
            conv_start = pd.to_datetime(row['conv_start_date'])
            days_from_conv = (evt_dt - conv_start).days
        else:
            days_from_conv = np.nan
        
        # 距到期天数
        if pd.notna(row.get('maturity_date')):
            maturity = pd.to_datetime(row['maturity_date'])
            days_to_mat = (maturity - evt_dt).days
        else:
            days_to_mat = np.nan
        
        feature_rows.append({
            'ts_code': code,
            'event_date': row['event_date'],
            'is_call': row['is_call'],
            'label': 1 if row['is_call'] == '公告实施强赎' else 0,
            # 特征
            'shd_ratio_pct': row['shd_ratio_pct'],
            'cb_over_rate_30d': cb_over_rate_30d,
            'close_cb_value_30d': close_cb_value_30d,
            'pct_chg_20d': pct_chg_20d,
            'pct_chg_60d': pct_chg_60d,
            'vol_ma5': vol_ma5,
            'remain_ratio': remain_ratio,
            'top1_hold_ratio': row.get('top1_hold_ratio', np.nan),
            'major_holder_cb_est': row.get('major_holder_cb_est', np.nan),
            'days_from_conv_start': days_from_conv,
            'days_to_maturity': days_to_mat,
            'coupon_rate': row.get('coupon_rate', np.nan),
        })
    
    panel = pd.DataFrame(feature_rows)
    print(f"面板: {len(panel)} 事件, {panel['label'].sum()} 强赎 / {len(panel)-panel['label'].sum()} 不强赎")
    
    return panel


def train_logit(panel, test_start_date='20240101'):
    """训练Logit模型 + OOS回测"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import accuracy_score, roc_auc_score, confusion_matrix
    
    # 特征列
    feature_cols = [
        'shd_ratio_pct', 'cb_over_rate_30d', 'close_cb_value_30d',
        'pct_chg_20d', 'pct_chg_60d', 'vol_ma5', 'remain_ratio',
        'top1_hold_ratio', 'major_holder_cb_est',
        'days_from_conv_start', 'days_to_maturity', 'coupon_rate'
    ]
    
    # 只保留有完整特征的样本
    panel_clean = panel.dropna(subset=feature_cols).copy()
    print(f"有完整特征的样本: {len(panel_clean)} / {len(panel)}")
    
    # 时间分割：2024年之前训练，之后OOS回测
    panel_clean['event_dt'] = pd.to_datetime(panel_clean['event_date'])
    train = panel_clean[panel_clean['event_dt'] < test_start_date]
    oos = panel_clean[panel_clean['event_dt'] >= test_start_date]
    
    print(f"\n训练集: {len(train)} ({train['label'].sum()} 强赎)")
    print(f"OOS集: {len(oos)} ({oos['label'].sum()} 强赎)")
    
    X_train = train[feature_cols].values
    y_train = train['label'].values
    X_oos = oos[feature_cols].values
    y_oos = oos['label'].values
    
    # 标准化
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_oos_scaled = scaler.transform(X_oos)
    
    # 训练模型（带L2正则）
    clf = LogisticRegression(C=1.0, class_weight='balanced', random_state=42, max_iter=1000)
    clf.fit(X_train_scaled, y_train)
    
    # 特征重要性
    coef_df = pd.DataFrame({
        'feature': feature_cols,
        'coef': clf.coef_[0],
        'abs_coef': np.abs(clf.coef_[0])
    }).sort_values('abs_coef', ascending=False)
    
    print(f"\n=== 特征重要性 ===")
    for _, r in coef_df.iterrows():
        print(f"  {r['feature']:25s}: {r['coef']:+.4f}")
    
    # 训练集评估
    train_pred = clf.predict(X_train_scaled)
    train_prob = clf.predict_proba(X_train_scaled)[:, 1]
    
    print(f"\n=== 训练集 ===")
    print(f"  AUC: {roc_auc_score(y_train, train_prob):.4f}")
    print(f"  Acc: {accuracy_score(y_train, train_pred):.4f}")
    tn, fp, fn, tp = confusion_matrix(y_train, train_pred).ravel()
    print(f"  TP={tp} FP={fp} FN={fn} TN={tn}")
    
    # OOS评估
    oos_pred = clf.predict(X_oos_scaled)
    oos_prob = clf.predict_proba(X_oos_scaled)[:, 1]
    
    print(f"\n=== OOS回测 ===")
    print(f"  AUC: {roc_auc_score(y_oos, oos_prob):.4f}")
    print(f"  Acc: {accuracy_score(y_oos, oos_pred):.4f}")
    tn, fp, fn, tp = confusion_matrix(y_oos, oos_pred).ravel()
    print(f"  TP={tp} FP={fp} FN={fn} TN={tn}")
    
    # 策略模拟（假设做多不强赎 / 做空强赎）
    # 设置阈值调优
    thresholds = np.arange(0.3, 0.8, 0.05)
    best_t = 0.5
    best_acc = 0
    
    print(f"\n=== 阈值调优（OOS） ===")
    for t in thresholds:
        pred_t = (oos_prob >= t).astype(int)
        acc = accuracy_score(y_oos, pred_t)
        if acc > best_acc:
            best_acc = acc
            best_t = t
        tn, fp, fn, tp = confusion_matrix(y_oos, pred_t).ravel()
        print(f"  thresh={t:.2f}: acc={acc:.4f} TP={tp} FP={fp} FN={fn} TN={tn}")
    
    print(f"\n最佳阈值: {best_t:.2f} (acc={best_acc:.4f})")
    
    return clf, scaler, feature_cols, best_t, oos_prob, y_oos


# ===========================================================================
# 入口
# ===========================================================================

if __name__ == '__main__':
    daily, basic, call, df_issue, top1 = load_all_data()
    panel = build_event_panel(daily, basic, call, df_issue, top1)
    
    # 保存面板
    panel.to_parquet('/home/jay/projects/quant/data/cb_warehouse/event_panel.parquet')
    print(f"\n面板已保存: event_panel.parquet ({len(panel)} 行)")
    
    # 训练模型
    model, scaler, features, best_thresh, oos_prob, y_oos = train_logit(panel)
    
    # 保存模型
    import joblib
    model_dir = '/home/jay/projects/quant/strategies/cb_redemption'
    joblib.dump({
        'model': model,
        'scaler': scaler,
        'features': features,
        'best_threshold': best_thresh,
        'train_date': datetime.now().strftime('%Y%m%d')
    }, f'{model_dir}/logit_model.pkl')
    
    print(f"\n模型已保存: logit_model.pkl")
    
    # 统计强赎/不强赎的次数和比例 - 按月统计
    panel['month'] = pd.to_datetime(panel['event_date']).dt.to_period('M')
    monthly = panel.groupby(['month', 'label']).size().unstack(fill_value=0)
    print(f"\n=== 月度事件分布 ===")
    print(monthly.tail(24).to_string())
