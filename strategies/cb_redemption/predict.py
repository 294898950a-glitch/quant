"""
可转债强赎博弈 — 实时推理监视器

每10分钟运行一次：
1. 从仓库加载当前满足强赎条件的转债
2. 用预训练 Logit 模型推理每个转债的强赎概率
3. 输出高风险/低风险清单到同目录
"""

import warnings
warnings.filterwarnings("ignore")

import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from pathlib import Path
import sys

MODEL_DIR = Path('/home/jay/projects/quant/strategies/cb_redemption')
WAREHOUSE = Path('/home/jay/projects/quant/data/cb_warehouse')

def load_model():
    """加载训练好的 Logit 模型"""
    import joblib
    model_path = MODEL_DIR / 'logit_model.pkl'
    if not model_path.exists():
        print(f"[WARN] 模型文件不存在: {model_path}")
        return None
    data = joblib.load(model_path)
    return data

def load_latest_snapshots():
    """加载当前所有满足条件的可转债最新快照"""
    from strategies.cb_redemption.data import load_data_for_signals
    try:
        df = load_data_for_signals()
    except:
        # 回退方案：直接从仓库加载
        daily = pd.read_parquet(WAREHOUSE / 'cb_daily.parquet')
        basic = pd.read_parquet(WAREHOUSE / 'cb_basic.parquet')
        call = pd.read_parquet(WAREHOUSE / 'cb_call.parquet')
        
        # 找每个转债的最新行情
        daily = daily.sort_values(['ts_code', 'trade_date'])
        latest = daily.groupby('ts_code').last().reset_index()
        
        # 合并基本面
        result = latest.merge(basic, on='ts_code', how='left', suffixes=('_daily', '_basic'))
        
        # 补充配售数据
        import tushare as ts
        API_TOKEN = os.environ['TUSHARE_TOKEN']
        API_URL = os.environ.get('TUSHARE_HTTP_URL', 'http://tsy.xiaodefa.cn')
        ts.set_token(API_TOKEN)
        pro = ts.pro_api()
        pro._DataApi__http_url = API_URL
        df_issue = pro.cb_issue()
        
        result = result.merge(df_issue[['ts_code','shd_ration_size','issue_size']], on='ts_code', how='left')
        
        # 加载top1
        try:
            top1 = pd.read_parquet(WAREHOUSE / 'cb_top1_holder_est.parquet')
            result = result.merge(top1[['ts_code','top1_ratio','top1_name']].drop_duplicates('ts_code'), 
                                  on='ts_code', how='left')
        except:
            pass
        
        return result
    
    return df


def prepare_inference_features(df, model_data):
    """为当前数据准备特征矩阵"""
    features = model_data['features']
    
    # 计算特征
    feature_values = {}
    
    for feat in features:
        if feat == 'shd_ratio_pct':
            df['issue_size_e8'] = df['issue_size'] * 1e8
            df['shd_ratio_pct'] = df['shd_ration_size'] / df['issue_size_e8'].replace(0, np.nan) * 100
            feature_values[feat] = df['shd_ratio_pct']
        
        elif feat == 'cb_over_rate_30d':
            feature_values[feat] = df.get('cb_over_rate', np.nan)
        
        elif feat == 'close_cb_value_30d':
            feature_values[feat] = df['close'] / df['cb_value'].replace(0, np.nan)
        
        elif feat == 'pct_chg_20d':
            feature_values[feat] = df.get('pct_chg', np.nan)
        
        elif feat == 'pct_chg_60d':
            feature_values[feat] = df.get('pct_chg', np.nan)
        
        elif feat == 'vol_ma5':
            feature_values[feat] = df.get('vol', np.nan)
        
        elif feat == 'remain_ratio':
            # cmc_amt 或 remain_size / issue_size
            feature_values[feat] = df.get('remain_size', df.get('cmc_amt', np.nan)) / df.get('issue_size', 1)
        
        elif feat == 'top1_hold_ratio':
            feature_values[feat] = df.get('top1_ratio', np.nan)
        
        elif feat == 'major_holder_cb_est':
            shd = df.get('shd_ratio_pct', np.nan)
            top1 = df.get('top1_ratio', np.nan)
            feature_values[feat] = shd * top1 / 100 if (pd.notna(shd) and pd.notna(top1)) else np.nan
        
        elif feat == 'days_from_conv_start':
            # 从转股起始日到现在的天数
            conv_start = pd.to_datetime(df.get('conv_start_date'), errors='coerce')
            today = pd.Timestamp.now()
            feature_values[feat] = (today - conv_start).dt.days
        
        elif feat == 'days_to_maturity':
            maturity = pd.to_datetime(df.get('maturity_date'), errors='coerce')
            today = pd.Timestamp.now()
            feature_values[feat] = (maturity - today).dt.days
        
        elif feat == 'coupon_rate':
            feature_values[feat] = df.get('coupon_rate', np.nan)
        
        else:
            feature_values[feat] = np.nan
    
    # 构建特征矩阵
    X = pd.DataFrame(feature_values)
    return X[features]  # 保持列顺序


def predict_redemption():
    """主推理函数"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 开始强赎概率推理...")
    
    model_data = load_model()
    if model_data is None:
        print("模型未训练，跳过推理")
        return
    
    model = model_data['model']
    scaler = model_data['scaler']
    features = model_data['features']
    threshold = model_data.get('best_threshold', 0.5)
    train_date = model_data.get('train_date', 'unknown')
    
    print(f"  模型训练日期: {train_date}, 阈值: {threshold:.2f}")
    
    # 加载数据
    df = load_latest_snapshots()
    print(f"  加载转债数据: {len(df)} 只")
    
    # 筛选满足强赎条件的转债（cb_over_rate <= 0 且 close >= conv_price × 1.3）
    # 简化：用 cb_over_rate <= 0 作为条件
    if 'cb_over_rate' in df.columns:
        cond = df[((df['cb_over_rate'] <= 5) | df['cb_over_rate'].isna())].copy()
    else:
        cond = df.copy()
    
    print(f"  满足条件（溢价率≤5%）: {len(cond)} 只")
    
    if len(cond) == 0:
        print("  无满足条件的转债")
        return
    
    # 推理
    X = prepare_inference_features(cond, model_data)
    X_clean = X.dropna()
    
    if len(X_clean) == 0:
        print("  特征不全，无法推理")
        return
    
    # 标准化并预测
    X_scaled = scaler.transform(X_clean.values)
    probs = model.predict_proba(X_scaled)[:, 1]
    
    # 结果组装
    results = cond.loc[X_clean.index].copy()
    results['强赎概率'] = probs
    results['风险等级'] = pd.cut(
        probs, 
        bins=[0, 0.3, 0.5, 0.7, 1.0], 
        labels=['🟢低风险', '🟡中风险', '🟠较高风险', '🔴高风险']
    )
    
    # 排序
    results = results.sort_values('强赎概率', ascending=False)
    
    # 只展示高风险和中高风险
    high_risk = results[results['强赎概率'] >= threshold]
    low_risk = results[results['强赎概率'] < threshold]
    
    print(f"\n  🔴 高风险（概率≥{threshold:.0%}）: {len(high_risk)} 只")
    print(f"  🟢 低风险（概率<{threshold:.0%}）: {len(low_risk)} 只")
    
    # 输出top10
    print(f"\n  === 高风险 TOP 10 ===")
    for _, r in high_risk.head(10).iterrows():
        name = r.get('stk_short_name', r.get('bond_short_name', r['ts_code']))
        print(f"    {name:12s} | {r['强赎概率']:.1%} | 溢价率={r.get('cb_over_rate', '?'):.1f}%")
    
    # 保存结果到文件
    output_path = MODEL_DIR / 'inference_result.csv'
    results[['ts_code','强赎概率','风险等级'] + 
            [c for c in ['bond_short_name','stk_short_name','cb_over_rate','close'] 
             if c in results.columns]].to_csv(output_path, index=False)
    print(f"\n  结果已保存: {output_path}")
    
    return results


if __name__ == '__main__':
    predict_redemption()
