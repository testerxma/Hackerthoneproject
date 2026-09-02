//+------------------------------------------------------------------+
//|                                              SpeedTraderBot.mq5   |
//|     Speed Trader Bot v6.1 - Single-File MT5 Expert Advisor        |
//|                                                                  |
//|  Implements PLAN_v6.1_MASTER.md in full:                         |
//|   - 14 strategies (S1..S14); S11 deferred/disabled by default    |
//|   - 44 improvements (#1..#44); #22/#32 deferred                  |
//|   - One TimeEngine / RiskManager / ExitManager (single authority)|
//|   - Adaptive layers in Shadow Mode by default                    |
//|   - News filter, daily profit lock, exec-quality, health watchdog|
//|   - Strategy decay monitor, state reconciliation, recovery ladder|
//|   - Telegram (commands + inline buttons) + on-chart dashboard     |
//|                                                                  |
//|  North Star: maximum profit with minimum drawdown.               |
//|  Engineering guidance only - NOT financial advice.               |
//+------------------------------------------------------------------+
#property copyright "Speed Trader Bot"
#property version   "6.10"
#property description "Single-file multi-symbol speed-trading EA (v6.1 master plan)."

#include <Trade/Trade.mqh>

//==================================================================//
// SECTION 1 - CONSTANTS & ENUMS                                    //
//==================================================================//
#define EA_TAG              "STB61"
#define NUM_STRATEGIES      14
#define MAX_SYMBOLS         32
#define STATE_FILE_MAGIC    0x53544236
#define STATE_FILE_VERSION  3
#define MAX_LIQ_LEVELS      20
#define BB_WIDTH_HISTORY    60
#define SCORE_BONUS_CAP     30.0
#define R30                 30        // rolling window for strategy-decay PF

// Compact engine regime (for EV multipliers)
enum ENUM_REGIME { REGIME_RANGING=0, REGIME_TREND=1, REGIME_STRONG_TREND=2 };

// 8-state strategy-gating regime (from v5.1)
enum ENUM_MKT_STATE
{
   MKT_RANGING=0, MKT_WEAK_UP=1, MKT_WEAK_DOWN=2, MKT_STRONG_UP=3,
   MKT_STRONG_DOWN=4, MKT_QUIET=5, MKT_CHOPPY=6, MKT_VOLATILE=7
};

enum ENUM_LAYER_STATE { LAYER_SHADOW=0, LAYER_ACTIVE=1 };

//==================================================================//
// SECTION 2 - INPUTS                                               //
//==================================================================//
input group "=== Symbols ==="
input string InpSymbols          = "EURUSD,GBPUSD,USDJPY,USDCHF,USDCAD,AUDUSD,NZDUSD,EURGBP,EURJPY,GBPJPY,AUDJPY,XAUUSD";

input group "=== Risk Management ==="
input double InpRiskPerTrade     = 1.0;
input double InpDailyLossLimit   = 3.0;
input double InpWeeklyLossLimit  = 6.0;
input int    InpMaxConsecLosses  = 6;
input double InpPortfolioHeatMax = 8.0;
input double InpHeatReduceLevel  = 6.0;
input double InpCurrencyExpMax    = 4.0;
input double InpMinLotMult       = 0.25;
input double InpMaxLotMult       = 1.50;
input bool   InpUseKelly         = true;   // #8 fractional Kelly sizing (Shadow-safe)
input double InpKellyFraction    = 0.35;   // fraction of full Kelly

input group "=== Execution ==="
input long   InpMagicBase        = 6100000;
input int    InpMaxRetries       = 3;
input int    InpRetryDelaySec    = 2;
input double InpMaxSpreadPips     = 4.0;
input int    InpSlippagePoints   = 20;
input bool   InpUseAsyncExec     = false;  // #1 (off by default for broad compatibility)

input group "=== Scoring / Signals ==="
input double InpMinScore         = 55.0;
input int    InpFreshnessBars    = 3;
input int    InpTimerSeconds     = 2;
input double InpMinNetRRpips     = 5.0;    // reject signals with tiny SL/TP

input group "=== TimeEngine ==="
input int    InpBrokerGMTOffset  = 2;
input bool   InpUseDST           = true;
input int    InpFridayCloseHour  = 20;
input bool   InpBlockBadHours    = true;

input group "=== Adaptive Layers (Shadow Mode) ==="
input bool   InpShadowMode       = true;
input int    InpPromoteMinSample = 30;

input group "=== Strategy Enables ==="
input bool   InpEnableS1  = true;
input bool   InpEnableS2  = true;
input bool   InpEnableS3  = true;
input bool   InpEnableS4  = true;
input bool   InpEnableS5  = true;
input bool   InpEnableS6  = true;
input bool   InpEnableS7  = true;
input bool   InpEnableS8  = true;
input bool   InpEnableS9  = true;
input bool   InpEnableS10 = true;
input bool   InpEnableS11 = false;  // Harmonic - DEFERRED
input bool   InpEnableS12 = true;
input bool   InpEnableS13 = true;
input bool   InpEnableS14 = true;

input group "=== Improvements (#22/#32 deferred) ==="
input bool   InpUsePyramiding    = false;
input bool   InpUseCompounding   = false;
input bool   InpUseExitManager   = true;
input bool   InpUseSwapFilter    = true;
input bool   InpUseCorrelation   = true;

input group "=== Improvements #33-#44 ==="
input bool   InpUseNewsFilter    = true;
input int    InpNewsBeforeMin    = 15;
input int    InpNewsAfterMin     = 10;
input double InpDailyProfitTarget= 4.0;
input double InpDailyGiveback    = 1.5;
input bool   InpUseHealthWatchdog= true;
input bool   InpDecayMonitor     = true;
input double InpDecayPFFloor     = 0.90;
input int    InpDecayMinTrades   = 30;
input double InpFlashSpikeATR    = 3.0;
input int    InpStaleBars        = 24;
input double InpStaleProgressR   = 0.30;
input double InpSymbolPauseDD    = 4.0;
input int    InpSymbolPauseHours = 12;
input double InpRecoveryDDTrigger= 10.0;
input double InpRecoveryRiskMult = 0.5;

input group "=== Interface ==="
input bool   InpUseDashboard     = true;
input bool   InpUseTelegram      = false;  // set token/chat below to enable
input string InpTelegramToken    = "";
input string InpTelegramChatID   = "";
input bool   InpUseTelegramButtons = true;

input group "=== Logging ==="
input bool   InpVerboseLog       = true;
input bool   InpWriteCSV         = true;

//==================================================================//
// SECTION 3 - GLOBAL STRUCTS                                       //
//==================================================================//
struct LiquidityLevel
{
   double   price;
   bool     isHigh;
   int      touchCount;
   datetime lastTouch;
   bool     swept;
   bool     used;
};

struct SymbolState
{
   string   name;
   int      digits;
   double   point;
   double   pip;
   double   tickValue;
   double   tickSize;
   bool     ready;
   bool     active;          // false => auto-paused (#43)
   datetime pauseUntil;      // #43
   double   rollPnl;         // #43 rolling P/L (decayed)

   // --- H1 indicator handles ---
   int      hRSI, hEMA8, hEMA21, hEMA55, hEMA200, hATR, hADX, hBB, hIchi, hMACD;
   // --- M30 handles ---
   int      hRSI_M30, hEMA8_M30, hEMA21_M30;
   // --- H4 handles ---
   int      hEMA200_H4, hEMA50_H4;

   // --- cached H1 values (completed bar = shift 1) ---
   double   rsi, rsiPrev;
   double   ema8, ema21, ema55, ema200;
   double   atr, atrPrev;
   double   adx, diPlus, diMinus;
   double   bbUp, bbMid, bbLow, bbWidth;
   double   macdMain, macdSig, macdMainPrev, macdSigPrev;
   ENUM_REGIME   regime;
   ENUM_MKT_STATE state;
   int      trendDir;

   // --- squeeze (#27) ---
   double   bbWidthHist[BB_WIDTH_HISTORY];
   int      bbWidthCount;
   bool     squeezeActive;

   // --- Ichimoku (S9) ---
   double   ichiTenkan, ichiKijun, ichiSenkouA, ichiSenkouB, ichiChikou;
   double   cloudTop, cloudBottom, cloudThicknessPips;
   int      cloudColor;

   // --- Fib (#29) ---
   double   fibHigh, fib618, fib500, fib382, fibLow;
   bool     fibValid;

   // --- ORB (#31) ---
   double   orbHigh, orbLow;
   bool     orbSet;
   int      orbDay;

   // --- liquidity (S12/S14) ---
   LiquidityLevel liq[MAX_LIQ_LEVELS];
   int      liqCount;

   // --- VWAP (S2) ---
   double   vwap;
   bool     vwapValid;

   // --- exec quality per hour (#35) ---
   double   slipSum[24];
   int      slipCount[24];

   // --- bar gating ---
   datetime lastH1Bar;
   datetime lastM30Bar;
};

struct TradeSignal
{
   bool     valid;
   int      type;
   int      symIdx;
   int      stratIdx;
   double   entry;
   double   sl;
   double   tp;
   double   slPips;
   double   tpPips;
   double   baseScore;
   double   bonus;
   double   totalScore;
   double   estWinRate;
   double   estAvgWin;
   double   estAvgLoss;
   double   costPips;
   double   expectedValue;
   double   combinedPriority;
   datetime detectedAt;
   double   refPrice;
   string   breakdown;
};

struct StratStats
{
   int      trades;
   int      wins;
   double   sumWinPips;
   double   sumLossPips;
   double   winRate;
   double   avgWinPips;
   double   avgLossPips;
   datetime lastLossTime;
   int      recentLosses;
   // rolling decay window (#38)
   double   last30R[R30];
   int      r30idx;
   int      r30count;
   bool     demoted;          // #38 auto-demoted to Shadow
};

struct PendingOrder
{
   bool     active;
   TradeSignal sig;
   int      attempts;
   datetime nextTry;
};

//==================================================================//
// SECTION 4 - GLOBALS                                              //
//==================================================================//
CTrade        g_trade;
SymbolState   g_sym[];
int           g_symCount = 0;

StratStats    g_stats[NUM_STRATEGIES];
PendingOrder  g_pending[MAX_SYMBOLS];

bool          g_isTester  = false;
int           g_csvHandle = INVALID_HANDLE;

double        g_dayStartEquity  = 0.0;
double        g_weekStartEquity = 0.0;
double        g_dayPeakEquity    = 0.0;   // #34
double        g_equityPeak       = 0.0;   // #44 high-water mark
int           g_curDayOfYear    = -1;
int           g_curWeek         = -1;
int           g_accountConsecLosses = 0;
bool          g_haltedDaily      = false;
bool          g_haltedWeekly     = false;
bool          g_profitLocked     = false; // #34
bool          g_recoveryMode     = false; // #44
bool          g_paused           = false; // manual (Telegram)
bool          g_healthOK         = true;  // #36

ENUM_LAYER_STATE g_perfMatrixState   = LAYER_SHADOW;
ENUM_LAYER_STATE g_lossClusterState  = LAYER_SHADOW;
ENUM_LAYER_STATE g_adaptiveScoreState= LAYER_SHADOW;
ENUM_LAYER_STATE g_equityCurveState  = LAYER_SHADOW;
ENUM_LAYER_STATE g_dayHourState      = LAYER_SHADOW;

long          g_tgLastUpdateId   = 0;     // Telegram getUpdates offset

struct TimeEngineState
{
   datetime gmt;
   int      gmtHour;
   int      dayOfWeek;
   bool     fridayClose;
   bool     weekend;
   bool     badHour;
   bool     londonOpen;
   bool     newYorkOpen;
   bool     overlap;
   int      sessionIdx;   // 0 Asian, 1 London, 2 NY
};
TimeEngineState g_time;

//==================================================================//
// SECTION 5 - SMALL UTILITIES                                      //
//==================================================================//
void Log(const string m)        { if(InpVerboseLog) Print(EA_TAG," | ",m); }
void LogAlways(const string m)  { Print(EA_TAG," | ",m); }
double Clamp(double v,double lo,double hi){ return (v<lo?lo:(v>hi?hi:v)); }

int StrategyClass(int s)
{
   switch(s+1)
   {
      case 1: case 2: case 6: return 0; // mean reversion
      case 3: case 5: case 9: return 1; // trend
      case 7: case 10: case 13: return 2; // momentum/breakout
      default: return 3; // structural
   }
}

double RegimeMultiplier(int s, ENUM_REGIME r)
{
   int c = StrategyClass(s);
   if(c==0){ if(r==REGIME_RANGING) return 1.10; if(r==REGIME_STRONG_TREND) return 0.70; return 0.95; }
   if(c==1 || c==2){ if(r==REGIME_STRONG_TREND) return 1.15; if(r==REGIME_TREND) return 1.05; return 0.80; }
   return 1.0;
}

bool StrategyEnabled(int s)
{
   switch(s+1)
   {
      case 1: return InpEnableS1;  case 2: return InpEnableS2;  case 3: return InpEnableS3;
      case 4: return InpEnableS4;  case 5: return InpEnableS5;  case 6: return InpEnableS6;
      case 7: return InpEnableS7;  case 8: return InpEnableS8;  case 9: return InpEnableS9;
      case 10:return InpEnableS10; case 11:return InpEnableS11; case 12:return InpEnableS12;
      case 13:return InpEnableS13; case 14:return InpEnableS14;
   }
   return false;
}

long MagicFor(int s,int sym){ return InpMagicBase + (long)(s*100) + (long)sym; }
void DecodeMagic(long magic,int &s,int &sym){ long r=magic-InpMagicBase; s=(int)(r/100); sym=(int)(r%100); }

//==================================================================//
// SECTION 6 - SYMBOL MANAGER                                       //
//==================================================================//
double SymbolPip(int i){ return g_sym[i].pip; }

string BaseCurrency(int i)
{
   string c = SymbolInfoString(g_sym[i].name, SYMBOL_CURRENCY_BASE);
   if(c=="") c = StringSubstr(g_sym[i].name,0,3);
   return c;
}
string QuoteCurrency(int i)
{
   string c = SymbolInfoString(g_sym[i].name, SYMBOL_CURRENCY_PROFIT);
   if(c=="") c = StringSubstr(g_sym[i].name,3,3);
   return c;
}

double NormalizePrice(int i,double p)
{
   double t = g_sym[i].tickSize;
   if(t<=0.0) return NormalizeDouble(p, g_sym[i].digits);
   return NormalizeDouble(MathRound(p/t)*t, g_sym[i].digits);
}

double NormalizeLot(const string sym,double lot)
{
   double mn=SymbolInfoDouble(sym,SYMBOL_VOLUME_MIN);
   double mx=SymbolInfoDouble(sym,SYMBOL_VOLUME_MAX);
   double st=SymbolInfoDouble(sym,SYMBOL_VOLUME_STEP);
   if(st<=0.0) st=0.01;
   lot=MathFloor(lot/st)*st;
   lot=Clamp(lot,mn,mx);
   return NormalizeDouble(lot,2);
}

double PipValuePerLot(int i)
{
   double tv=g_sym[i].tickValue, ts=g_sym[i].tickSize;
   if(ts<=0.0) return 0.0;
   return tv*(g_sym[i].pip/ts);
}

double SpreadPips(int i)
{
   double a=SymbolInfoDouble(g_sym[i].name,SYMBOL_ASK);
   double b=SymbolInfoDouble(g_sym[i].name,SYMBOL_BID);
   if(g_sym[i].pip<=0.0) return 0.0;
   return (a-b)/g_sym[i].pip;
}

double StopsLevelPrice(int i)
{
   long lvl = SymbolInfoInteger(g_sym[i].name, SYMBOL_TRADE_STOPS_LEVEL);
   return (double)lvl * g_sym[i].point;
}

//==================================================================//
// SECTION 7 - TIMEENGINE                                           //
//==================================================================//
bool IsDST(datetime t)
{
   MqlDateTime d; TimeToStruct(t,d);
   if(d.mon>3 && d.mon<10) return true;
   if(d.mon<3 || d.mon>10) return false;
   int lastSunday = d.day - d.day_of_week;
   if(d.mon==3)  return (lastSunday>=25);
   if(d.mon==10) return (lastSunday<25);
   return false;
}

void UpdateTimeEngine()
{
   datetime broker=TimeCurrent();
   int off=InpBrokerGMTOffset;
   if(InpUseDST && IsDST(broker)) off+=1;
   g_time.gmt = broker - (datetime)(off*3600);
   MqlDateTime d; TimeToStruct(g_time.gmt,d);
   g_time.gmtHour=d.hour;
   g_time.dayOfWeek=d.day_of_week;
   g_time.weekend=(d.day_of_week==0 || d.day_of_week==6);
   g_time.fridayClose=(d.day_of_week==5 && d.hour>=InpFridayCloseHour);
   g_time.londonOpen=(d.hour>=7 && d.hour<16);
   g_time.newYorkOpen=(d.hour>=12 && d.hour<21);
   g_time.overlap=(d.hour>=12 && d.hour<16);
   g_time.badHour=(d.hour==22 || d.hour==23);
   if(d.hour>=7 && d.hour<12)       g_time.sessionIdx=1; // London
   else if(d.hour>=12 && d.hour<21) g_time.sessionIdx=2; // NY
   else                             g_time.sessionIdx=0; // Asian
}

bool TimeAllowsNewTrades()
{
   if(g_time.weekend) return false;
   if(g_time.fridayClose) return false;
   if(InpBlockBadHours && g_time.badHour) return false;
   return true;
}

//==================================================================//
// SECTION 8 - LOGGER / CSV                                         //
//==================================================================//
void OpenCSV()
{
   if(!InpWriteCSV || g_isTester) return;
   g_csvHandle=FileOpen("STB61_trades.csv", FILE_READ|FILE_WRITE|FILE_CSV|FILE_ANSI, ',');
   if(g_csvHandle!=INVALID_HANDLE)
   {
      FileSeek(g_csvHandle,0,SEEK_END);
      if(FileTell(g_csvHandle)==0)
         FileWrite(g_csvHandle,"time","symbol","strategy","type","lot","entry","sl","tp","score","ev","breakdown");
   }
}

void CSVLogTrade(const TradeSignal &s,double lot)
{
   if(g_csvHandle==INVALID_HANDLE) return;
   FileWrite(g_csvHandle,
      TimeToString(TimeCurrent(),TIME_DATE|TIME_MINUTES|TIME_SECONDS),
      g_sym[s.symIdx].name, "S"+IntegerToString(s.stratIdx+1),
      (s.type==ORDER_TYPE_BUY?"BUY":"SELL"), DoubleToString(lot,2),
      DoubleToString(s.entry,g_sym[s.symIdx].digits),
      DoubleToString(s.sl,g_sym[s.symIdx].digits),
      DoubleToString(s.tp,g_sym[s.symIdx].digits),
      DoubleToString(s.totalScore,1), DoubleToString(s.expectedValue,2), s.breakdown);
   FileFlush(g_csvHandle);
}

//==================================================================//
// SECTION 9 - INDICATOR LAYER                                      //
//==================================================================//
double Cl(int i,int sh){ return iClose(g_sym[i].name,PERIOD_H1,sh); }
double Op(int i,int sh){ return iOpen (g_sym[i].name,PERIOD_H1,sh); }
double Hi(int i,int sh){ return iHigh (g_sym[i].name,PERIOD_H1,sh); }
double Lo(int i,int sh){ return iLow  (g_sym[i].name,PERIOD_H1,sh); }
long   Vol(int i,int sh){ return iTickVolume(g_sym[i].name,PERIOD_H1,sh); }
datetime Tm(int i,int sh){ return iTime(g_sym[i].name,PERIOD_H1,sh); }

double AvgVolume(int i,int count)
{
   long sum=0; int n=0;
   for(int k=1;k<=count;k++){ sum+=Vol(i,k); n++; }
   return (n==0?0.0:(double)sum/n);
}

bool CreateHandles(int i)
{
   string s=g_sym[i].name;
   g_sym[i].hRSI    = iRSI(s,PERIOD_H1,14,PRICE_CLOSE);
   g_sym[i].hEMA8   = iMA (s,PERIOD_H1,8,0,MODE_EMA,PRICE_CLOSE);
   g_sym[i].hEMA21  = iMA (s,PERIOD_H1,21,0,MODE_EMA,PRICE_CLOSE);
   g_sym[i].hEMA55  = iMA (s,PERIOD_H1,55,0,MODE_EMA,PRICE_CLOSE);
   g_sym[i].hEMA200 = iMA (s,PERIOD_H1,200,0,MODE_EMA,PRICE_CLOSE);
   g_sym[i].hATR    = iATR(s,PERIOD_H1,14);
   g_sym[i].hADX    = iADX(s,PERIOD_H1,14);
   g_sym[i].hBB     = iBands(s,PERIOD_H1,20,0,2.0,PRICE_CLOSE);
   g_sym[i].hIchi   = iIchimoku(s,PERIOD_H1,9,26,52);
   g_sym[i].hMACD   = iMACD(s,PERIOD_H1,12,26,9,PRICE_CLOSE);
   g_sym[i].hRSI_M30  = iRSI(s,PERIOD_M30,14,PRICE_CLOSE);
   g_sym[i].hEMA8_M30 = iMA(s,PERIOD_M30,8,0,MODE_EMA,PRICE_CLOSE);
   g_sym[i].hEMA21_M30= iMA(s,PERIOD_M30,21,0,MODE_EMA,PRICE_CLOSE);
   g_sym[i].hEMA200_H4= iMA(s,PERIOD_H4,200,0,MODE_EMA,PRICE_CLOSE);
   g_sym[i].hEMA50_H4 = iMA(s,PERIOD_H4,50,0,MODE_EMA,PRICE_CLOSE);

   return (g_sym[i].hRSI!=INVALID_HANDLE && g_sym[i].hEMA8!=INVALID_HANDLE &&
           g_sym[i].hEMA21!=INVALID_HANDLE && g_sym[i].hEMA55!=INVALID_HANDLE &&
           g_sym[i].hEMA200!=INVALID_HANDLE && g_sym[i].hATR!=INVALID_HANDLE &&
           g_sym[i].hADX!=INVALID_HANDLE && g_sym[i].hBB!=INVALID_HANDLE &&
           g_sym[i].hIchi!=INVALID_HANDLE && g_sym[i].hMACD!=INVALID_HANDLE &&
           g_sym[i].hRSI_M30!=INVALID_HANDLE && g_sym[i].hEMA8_M30!=INVALID_HANDLE &&
           g_sym[i].hEMA21_M30!=INVALID_HANDLE && g_sym[i].hEMA200_H4!=INVALID_HANDLE &&
           g_sym[i].hEMA50_H4!=INVALID_HANDLE);
}

void ReleaseHandles(int i)
{
   IndicatorRelease(g_sym[i].hRSI);    IndicatorRelease(g_sym[i].hEMA8);
   IndicatorRelease(g_sym[i].hEMA21);  IndicatorRelease(g_sym[i].hEMA55);
   IndicatorRelease(g_sym[i].hEMA200); IndicatorRelease(g_sym[i].hATR);
   IndicatorRelease(g_sym[i].hADX);    IndicatorRelease(g_sym[i].hBB);
   IndicatorRelease(g_sym[i].hIchi);   IndicatorRelease(g_sym[i].hMACD);
   IndicatorRelease(g_sym[i].hRSI_M30);IndicatorRelease(g_sym[i].hEMA8_M30);
   IndicatorRelease(g_sym[i].hEMA21_M30);IndicatorRelease(g_sym[i].hEMA200_H4);
   IndicatorRelease(g_sym[i].hEMA50_H4);
}

bool ReadBuf(int handle,int buffer,int count,double &out[])
{
   ArraySetAsSeries(out,true);
   return (CopyBuffer(handle,buffer,0,count,out)==count);
}
bool ReadOne(int handle,int buffer,int shift,double &val)
{
   double tmp[]; ArraySetAsSeries(tmp,true);
   if(CopyBuffer(handle,buffer,0,shift+1,tmp)!=shift+1) return false;
   val=tmp[shift]; return true;
}

// 8-state regime detection
ENUM_MKT_STATE DetectMktState(int i, double atrNow, double atrAvg)
{
   SymbolState st=g_sym[i];
   if(atrAvg>0 && atrNow > 2.0*atrAvg) return MKT_VOLATILE;
   bool narrowBB = (st.bbMid>0 && st.bbWidth < 0.01);
   if(st.adx<15 && atrAvg>0 && atrNow<0.7*atrAvg && narrowBB) return MKT_QUIET;
   // direction changes over last 12 closes
   int changes=0; int prevDir=0;
   for(int k=2;k<=13;k++)
   {
      double diff=Cl(i,k-1)-Cl(i,k);
      int dir=(diff>0?1:(diff<0?-1:0));
      if(dir!=0 && prevDir!=0 && dir!=prevDir) changes++;
      if(dir!=0) prevDir=dir;
   }
   if(changes>=8 && st.adx<25) return MKT_CHOPPY;
   bool up=(st.ema8>st.ema55);
   if(st.adx>40) return (up?MKT_STRONG_UP:MKT_STRONG_DOWN);
   if(st.adx>20) return (up?MKT_WEAK_UP:MKT_WEAK_DOWN);
   return MKT_RANGING;
}

bool UpdateIndicators(int i)
{
   SymbolState st=g_sym[i];
   double rsi[3],e8[3],e21[3],e55[3],e200[3],atr[3];
   double adx[3],dip[3],dim[3],bbU[3],bbM[3],bbL[3],mM[3],mS[3];
   double iT[3],iK[3],iA[3],iB[3],iC[3];

   if(!ReadBuf(st.hRSI,0,3,rsi))  return false;
   if(!ReadBuf(st.hEMA8,0,3,e8))  return false;
   if(!ReadBuf(st.hEMA21,0,3,e21))return false;
   if(!ReadBuf(st.hEMA55,0,3,e55))return false;
   if(!ReadBuf(st.hEMA200,0,3,e200))return false;
   if(!ReadBuf(st.hATR,0,3,atr))  return false;
   if(!ReadBuf(st.hADX,0,3,adx))  return false;
   if(!ReadBuf(st.hADX,1,3,dip))  return false;
   if(!ReadBuf(st.hADX,2,3,dim))  return false;
   if(!ReadBuf(st.hBB,0,3,bbM))   return false;
   if(!ReadBuf(st.hBB,1,3,bbU))   return false;
   if(!ReadBuf(st.hBB,2,3,bbL))   return false;
   if(!ReadBuf(st.hMACD,0,3,mM))  return false;
   if(!ReadBuf(st.hMACD,1,3,mS))  return false;
   if(!ReadBuf(st.hIchi,0,3,iT))  return false;
   if(!ReadBuf(st.hIchi,1,3,iK))  return false;
   if(!ReadBuf(st.hIchi,2,3,iA))  return false;
   if(!ReadBuf(st.hIchi,3,3,iB))  return false;
   if(!ReadBuf(st.hIchi,4,3,iC))  return false;

   st.rsi=rsi[1]; st.rsiPrev=rsi[2];
   st.ema8=e8[1]; st.ema21=e21[1]; st.ema55=e55[1]; st.ema200=e200[1];
   st.atr=atr[1]; st.atrPrev=atr[2];
   st.adx=adx[1]; st.diPlus=dip[1]; st.diMinus=dim[1];
   st.bbUp=bbU[1]; st.bbMid=bbM[1]; st.bbLow=bbL[1];
   st.bbWidth=(st.bbMid>0.0?(st.bbUp-st.bbLow)/st.bbMid:0.0);
   st.macdMain=mM[1]; st.macdSig=mS[1]; st.macdMainPrev=mM[2]; st.macdSigPrev=mS[2];

   st.ichiTenkan=iT[1]; st.ichiKijun=iK[1]; st.ichiSenkouA=iA[1];
   st.ichiSenkouB=iB[1]; st.ichiChikou=iC[1];
   st.cloudTop=MathMax(st.ichiSenkouA,st.ichiSenkouB);
   st.cloudBottom=MathMin(st.ichiSenkouA,st.ichiSenkouB);
   st.cloudThicknessPips=(st.pip>0.0?(st.cloudTop-st.cloudBottom)/st.pip:0.0);
   st.cloudColor=(st.ichiSenkouA>st.ichiSenkouB?+1:-1);

   if(st.adx>=35.0) st.regime=REGIME_STRONG_TREND;
   else if(st.adx>=22.0) st.regime=REGIME_TREND;
   else st.regime=REGIME_RANGING;
   if(e8[1]>e8[2] && st.ema8>st.ema55) st.trendDir=+1;
   else if(e8[1]<e8[2] && st.ema8<st.ema55) st.trendDir=-1;
   else st.trendDir=0;

   if(st.bbWidthCount<BB_WIDTH_HISTORY){ st.bbWidthHist[st.bbWidthCount]=st.bbWidth; st.bbWidthCount++; }
   else { for(int k=0;k<BB_WIDTH_HISTORY-1;k++) st.bbWidthHist[k]=st.bbWidthHist[k+1]; st.bbWidthHist[BB_WIDTH_HISTORY-1]=st.bbWidth; }
   double mnW=st.bbWidth;
   for(int k=0;k<st.bbWidthCount;k++) if(st.bbWidthHist[k]<mnW) mnW=st.bbWidthHist[k];
   st.squeezeActive=(st.bbWidthCount>=20 && st.bbWidth<=mnW*1.10);

   g_sym[i]=st;
   // ATR avg for state detection
   double atrAvg=0; double atrHist[20];
   if(ReadBuf(st.hATR,0,20,atrHist)){ double s2=0; for(int k=0;k<20;k++) s2+=atrHist[k]; atrAvg=s2/20.0; }
   g_sym[i].state=DetectMktState(i, st.atr, atrAvg);
   g_sym[i].ready=true;
   return true;
}

void UpdateFib(int i,int lookback=50)
{
   double hh=-DBL_MAX, ll=DBL_MAX;
   for(int k=1;k<=lookback;k++){ double h=Hi(i,k),l=Lo(i,k); if(h>hh)hh=h; if(l<ll)ll=l; }
   if(hh<=ll){ g_sym[i].fibValid=false; return; }
   double rng=hh-ll;
   g_sym[i].fibHigh=hh; g_sym[i].fibLow=ll;
   g_sym[i].fib382=hh-rng*0.382; g_sym[i].fib500=hh-rng*0.5; g_sym[i].fib618=hh-rng*0.618;
   g_sym[i].fibValid=true;
}

void UpdateVWAP(int i)
{
   MqlDateTime now; TimeToStruct(TimeCurrent(),now);
   double pv=0,vv=0;
   for(int k=1;k<=48;k++)
   {
      datetime bt=Tm(i,k); if(bt==0) break;
      MqlDateTime bd; TimeToStruct(bt,bd);
      if(bd.day!=now.day) break;
      double typ=(Hi(i,k)+Lo(i,k)+Cl(i,k))/3.0;
      double v=(double)Vol(i,k);
      pv+=typ*v; vv+=v;
   }
   if(vv>0){ g_sym[i].vwap=pv/vv; g_sym[i].vwapValid=true; }
   else g_sym[i].vwapValid=false;
}

void UpdateORB(int i)
{
   MqlDateTime d; TimeToStruct(Tm(i,1),d);
   if(g_sym[i].orbDay!=d.day_of_year)
   {
      g_sym[i].orbDay=d.day_of_year;
      g_sym[i].orbHigh=Hi(i,1); g_sym[i].orbLow=Lo(i,1); g_sym[i].orbSet=true;
   }
}

//==================================================================//
// SECTION 10 - LIQUIDITY LEVELS                                    //
//==================================================================//
void AddOrUpdateLevel(int i,double price,bool isHigh)
{
   double pip=SymbolPip(i);
   for(int k=0;k<g_sym[i].liqCount;k++)
      if(g_sym[i].liq[k].isHigh==isHigh && MathAbs(g_sym[i].liq[k].price-price)/pip<3.0)
      { g_sym[i].liq[k].touchCount++; g_sym[i].liq[k].lastTouch=TimeCurrent(); return; }
   int idx=g_sym[i].liqCount;
   if(idx>=MAX_LIQ_LEVELS)
   {
      int oldest=0;
      for(int k=1;k<MAX_LIQ_LEVELS;k++) if(g_sym[i].liq[k].lastTouch<g_sym[i].liq[oldest].lastTouch) oldest=k;
      idx=oldest;
   } else g_sym[i].liqCount++;
   g_sym[i].liq[idx].price=price; g_sym[i].liq[idx].isHigh=isHigh;
   g_sym[i].liq[idx].touchCount=1; g_sym[i].liq[idx].lastTouch=TimeCurrent();
   g_sym[i].liq[idx].swept=false; g_sym[i].liq[idx].used=false;
}

void UpdateLiquidityLevels(int i)
{
   double h[7],l[7];
   for(int k=0;k<7;k++){ h[k]=Hi(i,k+1); l[k]=Lo(i,k+1); }
   if(h[3]>h[2]&&h[3]>h[1]&&h[3]>h[0]&&h[3]>h[4]&&h[3]>h[5]&&h[3]>h[6]) AddOrUpdateLevel(i,h[3],true);
   if(l[3]<l[2]&&l[3]<l[1]&&l[3]<l[0]&&l[3]<l[4]&&l[3]<l[5]&&l[3]<l[6]) AddOrUpdateLevel(i,l[3],false);
   datetime cut=TimeCurrent()-48*3600;
   for(int k=0;k<g_sym[i].liqCount;k++) if(g_sym[i].liq[k].lastTouch<cut) g_sym[i].liq[k].used=true;
}

double NearestResistance(int i,double price)
{
   double best=0,bd=DBL_MAX;
   for(int k=0;k<g_sym[i].liqCount;k++){ if(!g_sym[i].liq[k].isHigh) continue; double p=g_sym[i].liq[k].price; if(p>price&&(p-price)<bd){bd=p-price;best=p;} }
   return best;
}
double NearestSupport(int i,double price)
{
   double best=0,bd=DBL_MAX;
   for(int k=0;k<g_sym[i].liqCount;k++){ if(g_sym[i].liq[k].isHigh) continue; double p=g_sym[i].liq[k].price; if(p<price&&(price-p)<bd){bd=price-p;best=p;} }
   return best;
}

//==================================================================//
// SECTION 11 - CANDLE HELPERS                                      //
//==================================================================//
bool IsBullishReversal(int i,int sh)
{
   double o=Op(i,sh),c=Cl(i,sh),h=Hi(i,sh),l=Lo(i,sh);
   double body=MathAbs(c-o), lw=MathMin(o,c)-l, rng=h-l;
   if(rng<=0) return false;
   bool pin=(lw>body*1.5 && c>o);
   bool eng=(c>o && c>Op(i,sh+1) && o<Cl(i,sh+1) && Cl(i,sh+1)<Op(i,sh+1));
   return pin||eng;
}
bool IsBearishReversal(int i,int sh)
{
   double o=Op(i,sh),c=Cl(i,sh),h=Hi(i,sh),l=Lo(i,sh);
   double body=MathAbs(c-o), uw=h-MathMax(o,c), rng=h-l;
   if(rng<=0) return false;
   bool pin=(uw>body*1.5 && c<o);
   bool eng=(c<o && c<Op(i,sh+1) && o>Cl(i,sh+1) && Cl(i,sh+1)>Op(i,sh+1));
   return pin||eng;
}

double CandleQualityPenalty(int i,int type)
{
   double o=Op(i,1),c=Cl(i,1),h=Hi(i,1),l=Lo(i,1), rng=h-l;
   if(rng<=0) return -10.0;
   double body=MathAbs(c-o), br=body/rng, pen=0;
   if(br<0.30) pen-=8.0; else if(br<0.50) pen-=4.0;
   if(type==ORDER_TYPE_BUY && c<o && br>0.6) pen-=4.0;
   if(type==ORDER_TYPE_SELL && c>o && br>0.6) pen-=4.0;
   return MathMax(pen,-10.0);
}

// Wick/body filter for breakouts (#20)
double WickPenalty(int i,int type)
{
   double o=Op(i,1),c=Cl(i,1),h=Hi(i,1),l=Lo(i,1);
   double body=MathAbs(c-o);
   if(body<=0) return -4.0;
   double upper=h-MathMax(o,c), lower=MathMin(o,c)-l;
   if(type==ORDER_TYPE_BUY && upper>body) return -5.0;   // long upper wick on up-break
   if(type==ORDER_TYPE_SELL && lower>body) return -5.0;
   return 0.0;
}

//==================================================================//
// SECTION 12 - SCORING FRAMEWORK                                   //
//==================================================================//
void AddScoreBonus(TradeSignal &sig,const string comp,double pts)
{
   if(pts==0.0) return;
   double before=sig.bonus;
   sig.bonus=Clamp(sig.bonus+pts,-50.0,SCORE_BONUS_CAP);
   double applied=sig.bonus-before;
   sig.breakdown+=StringFormat("%s%+.0f ",comp,applied);
}
void FinalizeScore(TradeSignal &sig){ sig.totalScore=sig.baseScore+sig.bonus; }

//==================================================================//
// SECTION 13 - EV LAYER (#1)                                       //
//==================================================================//
void ComputeEV(TradeSignal &sig)
{
   int si=sig.stratIdx;
   double wr=g_stats[si].winRate, tr=(double)g_stats[si].trades;
   if(tr<20){ sig.estWinRate=0.50; sig.estAvgWin=sig.tpPips; sig.estAvgLoss=sig.slPips; }
   else
   {
      double m=RegimeMultiplier(si,g_sym[sig.symIdx].regime);
      sig.estWinRate=Clamp(wr*m,0.15,0.85);
      sig.estAvgWin=(g_stats[si].avgWinPips>0?g_stats[si].avgWinPips:sig.tpPips);
      sig.estAvgLoss=(g_stats[si].avgLossPips>0?g_stats[si].avgLossPips:sig.slPips);
   }
   // measured slippage cost per hour (#35) + spread
   int h=g_time.gmtHour;
   double slip=0; if(h>=0&&h<24&&g_sym[sig.symIdx].slipCount[h]>0) slip=g_sym[sig.symIdx].slipSum[h]/g_sym[sig.symIdx].slipCount[h];
   sig.costPips=SpreadPips(sig.symIdx)+0.5+MathAbs(slip);
   sig.expectedValue=sig.estWinRate*sig.estAvgWin-(1.0-sig.estWinRate)*sig.estAvgLoss-sig.costPips;
   double normEV=Clamp(50.0+sig.expectedValue,0.0,100.0);
   sig.combinedPriority=sig.totalScore*0.5+normEV*0.5;
}

//==================================================================//
// SECTION 14 - BOOSTER FILTERS                                     //
//==================================================================//
void ApplyVolumeBonus(TradeSignal &sig)
{
   double avg=AvgVolume(sig.symIdx,20); if(avg<=0) return;
   double r=(double)Vol(sig.symIdx,1)/avg;
   if(r>1.5) AddScoreBonus(sig,"Vol",8); else if(r>1.2) AddScoreBonus(sig,"Vol",4);
}
void ApplySqueezeBonus(TradeSignal &sig){ if(g_sym[sig.symIdx].squeezeActive) AddScoreBonus(sig,"Squeeze",10); }
void ApplyFibBonus(TradeSignal &sig)
{
   if(!g_sym[sig.symIdx].fibValid) return;
   double pip=SymbolPip(sig.symIdx), tol=8.0*pip, p=sig.entry;
   if(MathAbs(p-g_sym[sig.symIdx].fib618)<tol || MathAbs(p-g_sym[sig.symIdx].fib500)<tol || MathAbs(p-g_sym[sig.symIdx].fib382)<tol)
      AddScoreBonus(sig,"Fib",8);
}
double CurrencyMomentum(const string ccy)
{
   double sc=0; int n=0;
   for(int i=0;i<g_symCount;i++)
   {
      if(!g_sym[i].ready) continue;
      int dir=(g_sym[i].ema8>g_sym[i].ema55?+1:-1);
      if(BaseCurrency(i)==ccy){ sc+=dir; n++; } else if(QuoteCurrency(i)==ccy){ sc-=dir; n++; }
   }
   return (n==0?0.0:sc/n);
}
void ApplyBasketBonus(TradeSignal &sig)
{
   double mom=CurrencyMomentum(BaseCurrency(sig.symIdx));
   bool al=(sig.type==ORDER_TYPE_BUY?mom>0.5:mom<-0.5);
   if(al) AddScoreBonus(sig,"Basket",6);
}
void ApplyORBBonus(TradeSignal &sig)
{
   if(!g_sym[sig.symIdx].orbSet) return;
   double pip=SymbolPip(sig.symIdx);
   if(sig.type==ORDER_TYPE_BUY && sig.entry>g_sym[sig.symIdx].orbHigh+pip) AddScoreBonus(sig,"ORB",8);
   if(sig.type==ORDER_TYPE_SELL && sig.entry<g_sym[sig.symIdx].orbLow-pip) AddScoreBonus(sig,"ORB",8);
}
void ApplyTimeCostBonus(TradeSignal &sig)
{
   if(g_time.overlap) AddScoreBonus(sig,"Overlap",5);
   else if(g_time.badHour) AddScoreBonus(sig,"BadHour",-8);
}
void ApplySwapBonus(TradeSignal &sig)
{
   if(!InpUseSwapFilter) return;
   double sw=(sig.type==ORDER_TYPE_BUY)?SymbolInfoDouble(g_sym[sig.symIdx].name,SYMBOL_SWAP_LONG):SymbolInfoDouble(g_sym[sig.symIdx].name,SYMBOL_SWAP_SHORT);
   if(sw>0) AddScoreBonus(sig,"Swap+",3); else if(sw<-3.0) AddScoreBonus(sig,"Swap-",-3);
}
void ApplyCandleBonus(TradeSignal &sig)
{
   double pen=CandleQualityPenalty(sig.symIdx,sig.type); if(pen!=0) AddScoreBonus(sig,"Candle",pen);
   int cls=StrategyClass(sig.stratIdx);
   if(cls==2){ double wp=WickPenalty(sig.symIdx,sig.type); if(wp!=0) AddScoreBonus(sig,"Wick",wp); }
}

void ScoreAndRank(TradeSignal &sig)
{
   ApplyCandleBonus(sig); ApplyVolumeBonus(sig); ApplySqueezeBonus(sig);
   ApplyFibBonus(sig); ApplyBasketBonus(sig); ApplyORBBonus(sig);
   ApplyTimeCostBonus(sig); ApplySwapBonus(sig);
   FinalizeScore(sig); ComputeEV(sig);
}

//==================================================================//
// SECTION 15 - SIGNAL CONSTRUCTION                                 //
//==================================================================//
void InitSignal(TradeSignal &sig,int i,int s,int type,double entry,double sl,double tp,double baseScore)
{
   sig.valid=true; sig.symIdx=i; sig.stratIdx=s; sig.type=type;
   sig.entry=NormalizePrice(i,entry); sig.sl=NormalizePrice(i,sl); sig.tp=NormalizePrice(i,tp);
   sig.baseScore=baseScore; sig.bonus=0; sig.totalScore=baseScore;
   double pip=SymbolPip(i);
   sig.slPips=MathAbs(sig.entry-sig.sl)/pip; sig.tpPips=MathAbs(sig.tp-sig.entry)/pip;
   sig.detectedAt=TimeCurrent(); sig.refPrice=sig.entry;
   sig.breakdown=StringFormat("base%.0f ",baseScore);
}

// M30 helpers
double M30_RSI(int i,int sh){ double v; return (ReadOne(g_sym[i].hRSI_M30,0,sh,v)?v:50.0); }
double M30_EMA8(int i,int sh){ double v; return (ReadOne(g_sym[i].hEMA8_M30,0,sh,v)?v:0.0); }
double M30_EMA21(int i,int sh){ double v; return (ReadOne(g_sym[i].hEMA21_M30,0,sh,v)?v:0.0); }
double H4_EMA200(int i){ double v; return (ReadOne(g_sym[i].hEMA200_H4,0,1,v)?v:0.0); }
double H4_EMA50(int i){ double v; return (ReadOne(g_sym[i].hEMA50_H4,0,1,v)?v:0.0); }
bool M30Bull(int i){ return iClose(g_sym[i].name,PERIOD_M30,1)>iOpen(g_sym[i].name,PERIOD_M30,1); }
bool M30Bear(int i){ return iClose(g_sym[i].name,PERIOD_M30,1)<iOpen(g_sym[i].name,PERIOD_M30,1); }

//==================================================================//
// SECTION 16 - STRATEGIES S1..S14                                  //
//==================================================================//
bool StateAllows(int i, int s)
{
   ENUM_MKT_STATE m=g_sym[i].state;
   switch(s+1)
   {
      case 1: return !(m==MKT_QUIET);                                   // S1: not Quiet
      case 2: return (m==MKT_RANGING||m==MKT_WEAK_UP||m==MKT_WEAK_DOWN);// S2: range/weak only
      case 3: return (m==MKT_STRONG_UP||m==MKT_STRONG_DOWN||m==MKT_WEAK_UP||m==MKT_WEAK_DOWN); // S3 trend
      case 4: return !(m==MKT_QUIET||m==MKT_CHOPPY);                    // S4
      case 5: return (m==MKT_STRONG_UP||m==MKT_STRONG_DOWN||m==MKT_WEAK_UP||m==MKT_WEAK_DOWN); // S5
      case 6: return !(m==MKT_QUIET);                                   // S6
      case 7: return !(m==MKT_QUIET||m==MKT_CHOPPY);                    // S7
      case 8: return true;                                             // S8 always (score-gated)
      default: return !(m==MKT_QUIET);                                  // S9..S14: avoid dead market
   }
}

// S1 RSI Reversal + EMA (M30 entry / H1 trend)
TradeSignal Strategy_S1(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   double price=Cl(i,1); double pip=st.pip;
   double r1=M30_RSI(i,1), r2=M30_RSI(i,2);
   if(r1<30.0 && r1>r2 && price>st.ema200 && M30Bull(i))      // oversold turning up
   { InitSignal(s,i,0,ORDER_TYPE_BUY,price,price-15*pip,price+30*pip,50.0); }
   else if(r1>70.0 && r1<r2 && price<st.ema200 && M30Bear(i))
   { InitSignal(s,i,0,ORDER_TYPE_SELL,price,price+15*pip,price-30*pip,50.0); }
   return s;
}

// S2 VWAP Bounce
TradeSignal Strategy_S2(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(!st.vwapValid) return s;
   double price=Cl(i,1), pip=st.pip, dist=(price-st.vwap)/pip;
   bool fastUp=(st.ema8>st.ema21);
   if(dist<-3.0 && M30Bull(i) && fastUp)
   { InitSignal(s,i,1,ORDER_TYPE_BUY,price,price-18*pip,price+36*pip,50.0); }
   else if(dist>3.0 && M30Bear(i) && !fastUp)
   { InitSignal(s,i,1,ORDER_TYPE_SELL,price,price+18*pip,price-36*pip,50.0); }
   return s;
}

// S3 Market Structure Break (BOS) - simplified two-phase (break + close-through)
TradeSignal Strategy_S3(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   if(Tm(i,12)==0) return s;   // need enough H1 history
   double hh=-DBL_MAX,ll=DBL_MAX;
   for(int k=2;k<=11;k++){ hh=MathMax(hh,Hi(i,k)); ll=MathMin(ll,Lo(i,k)); }
   double price=Cl(i,1), pip=st.pip;
   if(price>hh+2*pip && st.trendDir>0 && st.adx>=20.0)
   { double sl=ll-st.atr*0.3; double tp=price+(price-sl)*2.5; InitSignal(s,i,2,ORDER_TYPE_BUY,price,sl,tp,50.0); }
   else if(price<ll-2*pip && st.trendDir<0 && st.adx>=20.0)
   { double sl=hh+st.atr*0.3; double tp=price-(sl-price)*2.5; InitSignal(s,i,2,ORDER_TYPE_SELL,price,sl,tp,50.0); }
   return s;
}

// S4 Supply & Demand + FVG
TradeSignal Strategy_S4(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   double pip=st.pip, price=Cl(i,1);
   // impulse candle (>2xATR) sets zone; bar before impulse = zone
   double r0h=Hi(i,1),r0l=Lo(i,1),r2h=Hi(i,3),r2l=Lo(i,3);
   bool bullFVG=(r2l>r0h)&&((r2l-r0h)/pip>=5.0);
   bool bearFVG=(r0l>r2h)&&((r0l-r2h)/pip>=5.0);
   double support=NearestSupport(i,price), resist=NearestResistance(i,price);
   bool impulseUp=( (Cl(i,2)-Op(i,2)) > 2.0*st.atr );
   bool impulseDn=( (Op(i,2)-Cl(i,2)) > 2.0*st.atr );
   if(support>0 && (price-support)/pip<10.0 && IsBullishReversal(i,1) && st.rsi<60 && (impulseUp||bullFVG))
   {
      double sl=support-st.atr*0.5, tp=price+(price-sl)*2.0;
      InitSignal(s,i,3,ORDER_TYPE_BUY,price,sl,tp,50.0);
      if(bullFVG) AddScoreBonus(s,"FVG",15);
      if(bullFVG && st.fibValid && MathAbs(price-st.fib618)<8*pip) AddScoreBonus(s,"TripleConf",10);
   }
   else if(resist>0 && (resist-price)/pip<10.0 && IsBearishReversal(i,1) && st.rsi>40 && (impulseDn||bearFVG))
   {
      double sl=resist+st.atr*0.5, tp=price-(sl-price)*2.0;
      InitSignal(s,i,3,ORDER_TYPE_SELL,price,sl,tp,50.0);
      if(bearFVG) AddScoreBonus(s,"FVG",15);
      if(bearFVG && st.fibValid && MathAbs(price-st.fib618)<8*pip) AddScoreBonus(s,"TripleConf",10);
   }
   return s;
}

// S5 EMA Triple Crossover (8,21,55)
TradeSignal Strategy_S5(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   double price=Cl(i,1), pip=st.pip;
   bool bull=(st.ema8>st.ema21 && st.ema21>st.ema55);
   bool bear=(st.ema8<st.ema21 && st.ema21<st.ema55);
   if(bull && Lo(i,1)<=st.ema21 && Cl(i,1)>st.ema21 && st.adx>25.0 && price>st.ema200)
   { InitSignal(s,i,4,ORDER_TYPE_BUY,price,price-15*pip,price+30*pip,50.0); }
   else if(bear && Hi(i,1)>=st.ema21 && Cl(i,1)<st.ema21 && st.adx>25.0 && price<st.ema200)
   { InitSignal(s,i,4,ORDER_TYPE_SELL,price,price+15*pip,price-30*pip,50.0); }
   return s;
}

// S6 RSI Divergence (H1 divergence, M30 confirm)
TradeSignal Strategy_S6(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   double rsiArr[10]; if(!ReadBuf(st.hRSI,0,10,rsiArr)) return s;
   double price=Cl(i,1);
   double pl1=Lo(i,1),pl2=Lo(i,5), rl1=rsiArr[1],rl2=rsiArr[5];
   if(pl1<pl2 && rl1>rl2 && st.rsi<40.0 && M30Bull(i))
   { double sl=pl1-st.atr*0.5, tp=price+(price-sl)*2.5; InitSignal(s,i,5,ORDER_TYPE_BUY,price,sl,tp,50.0); }
   else
   {
      double ph1=Hi(i,1),ph2=Hi(i,5), rh1=rsiArr[1],rh2=rsiArr[5];
      if(ph1>ph2 && rh1<rh2 && st.rsi>60.0 && M30Bear(i))
      { double sl=ph1+st.atr*0.5, tp=price-(sl-price)*2.5; InitSignal(s,i,5,ORDER_TYPE_SELL,price,sl,tp,50.0); }
   }
   return s;
}

// S7 Momentum Breakout (ATR)
TradeSignal Strategy_S7(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   if(Tm(i,22)==0) return s;   // need enough H1 history
   double price=Cl(i,1);
   double hh=-DBL_MAX,ll=DBL_MAX;
   for(int k=2;k<=21;k++){ hh=MathMax(hh,Hi(i,k)); ll=MathMin(ll,Lo(i,k)); }
   double candle=MathAbs(Cl(i,1)-Op(i,1));
   if(price>hh && candle>1.5*st.atr && price>st.ema200 && st.diPlus>st.diMinus)
   { double sl=price-1.5*st.atr, tp=price+3.0*st.atr; InitSignal(s,i,6,ORDER_TYPE_BUY,price,sl,tp,50.0); }
   else if(price<ll && candle>1.5*st.atr && price<st.ema200 && st.diMinus>st.diPlus)
   { double sl=price+1.5*st.atr, tp=price-3.0*st.atr; InitSignal(s,i,6,ORDER_TYPE_SELL,price,sl,tp,50.0); }
   return s;
}

// S8 Multi-TF Confluence (>=5 of 7)
TradeSignal Strategy_S8(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   double price=Cl(i,1), pip=st.pip;
   double h4_200=H4_EMA200(i);
   double h4price=iClose(g_sym[i].name,PERIOD_H4,1);
   double m30e8=M30_EMA8(i,1), m30e21=M30_EMA21(i,1);
   double m30price=iClose(g_sym[i].name,PERIOD_M30,1);
   // BUY factors
   int up=0;
   if(h4price>h4_200) up++;
   if(st.ema8>st.ema21) up++;
   if(st.rsi>50 && st.rsi<70) up++;
   if(st.rsi>st.rsiPrev) up++;
   if(m30e8>m30e21) up++;
   if(M30Bull(i)) up++;
   if(IsBullishReversal(i,1)) up++;
   int dn=0;
   if(h4price<h4_200) dn++;
   if(st.ema8<st.ema21) dn++;
   if(st.rsi<50 && st.rsi>30) dn++;
   if(st.rsi<st.rsiPrev) dn++;
   if(m30e8<m30e21) dn++;
   if(M30Bear(i)) dn++;
   if(IsBearishReversal(i,1)) dn++;
   if(up>=5 && up>dn)
   { double sl=price-20*pip, tp=price+50*pip; InitSignal(s,i,7,ORDER_TYPE_BUY,price,sl,tp,55.0); }
   else if(dn>=5 && dn>up)
   { double sl=price+20*pip, tp=price-50*pip; InitSignal(s,i,7,ORDER_TYPE_SELL,price,sl,tp,55.0); }
   return s;
}

// S9 Ichimoku Kumo Breakout
TradeSignal Strategy_S9(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   if(Tm(i,26)==0) return s;   // need 26 bars for Chikou comparison
   double pp=Cl(i,2), cp=Cl(i,1), p26=Cl(i,26);
   bool buy=(pp<=st.cloudTop && cp>st.cloudTop) && (st.ichiTenkan>st.ichiKijun) && (st.cloudColor==+1) && (st.ichiChikou>p26) && (st.cloudThicknessPips>=5.0);
   bool sell=(pp>=st.cloudBottom && cp<st.cloudBottom) && (st.ichiTenkan<st.ichiKijun) && (st.cloudColor==-1) && (st.ichiChikou<p26) && (st.cloudThicknessPips>=5.0);
   if(buy)
   { double sl=MathMin(st.cloudBottom,st.ichiKijun)-st.atr*0.2, tp=cp+(cp-sl)*2.0; InitSignal(s,i,8,ORDER_TYPE_BUY,cp,sl,tp,50.0); if(st.cloudThicknessPips>20) AddScoreBonus(s,"Cloud",10); }
   else if(sell)
   { double sl=MathMax(st.cloudTop,st.ichiKijun)+st.atr*0.2, tp=cp-(sl-cp)*2.0; InitSignal(s,i,8,ORDER_TYPE_SELL,cp,sl,tp,50.0); if(st.cloudThicknessPips>20) AddScoreBonus(s,"Cloud",10); }
   return s;
}

// S10 Triangle Breakout
TradeSignal Strategy_S10(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   if(Tm(i,33)==0) return s;   // need enough H1 history for swings
   double pip=st.pip; double H[3],L[3]; int hc=0,lc=0;
   for(int k=3;k<=30 && (hc<3||lc<3);k++)
   {
      if(hc<3 && Hi(i,k)>Hi(i,k-1)&&Hi(i,k)>Hi(i,k+1)&&Hi(i,k)>Hi(i,k-2)&&Hi(i,k)>Hi(i,k+2)) H[hc++]=Hi(i,k);
      if(lc<3 && Lo(i,k)<Lo(i,k-1)&&Lo(i,k)<Lo(i,k+1)&&Lo(i,k)<Lo(i,k-2)&&Lo(i,k)<Lo(i,k+2)) L[lc++]=Lo(i,k);
   }
   if(hc<3||lc<3) return s;
   bool hd=(H[0]<H[1]&&H[1]<H[2]), hf=(MathAbs(H[0]-H[1])/pip<5&&MathAbs(H[1]-H[2])/pip<5);
   bool li=(L[0]>L[1]&&L[1]>L[2]), lf=(MathAbs(L[0]-L[1])/pip<5&&MathAbs(L[1]-L[2])/pip<5);
   int tt=0; if(hd&&li) tt=1; else if(hf&&li) tt=2; else if(hd&&lf) tt=3; else return s;
   double upper=H[0],lower=L[0], base=(H[2]-L[2])/pip;
   if(base<20) return s;
   double close=Cl(i,1);
   if(close>upper+2*pip && (tt==2||tt==1))
   { double sl=lower-st.atr*0.3, tp=close+base*pip; InitSignal(s,i,9,ORDER_TYPE_BUY,close,sl,tp,55.0); if(st.squeezeActive) AddScoreBonus(s,"Squeeze",15); }
   else if(close<lower-2*pip && (tt==3||tt==1))
   { double sl=upper+st.atr*0.3, tp=close-base*pip; InitSignal(s,i,9,ORDER_TYPE_SELL,close,sl,tp,55.0); if(st.squeezeActive) AddScoreBonus(s,"Squeeze",15); }
   return s;
}

// S11 Harmonic - DEFERRED
TradeSignal Strategy_S11(int i){ TradeSignal s; s.valid=false; return s; }

// S12 Liquidity Sweep
TradeSignal Strategy_S12(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   double high1=Hi(i,1),low1=Lo(i,1),close1=Cl(i,1),open1=Op(i,1);
   for(int k=0;k<g_sym[i].liqCount;k++)
   {
      if(g_sym[i].liq[k].swept||g_sym[i].liq[k].used) continue;
      double lvl=g_sym[i].liq[k].price; int tc=g_sym[i].liq[k].touchCount;
      if(g_sym[i].liq[k].isHigh)
      {
         if(high1>lvl && close1<lvl)
         {
            g_sym[i].liq[k].swept=true;
            double uw=high1-MathMax(open1,close1), body=MathAbs(close1-open1);
            if(uw<body*0.5) continue;
            double sl=high1+st.atr*0.3, tp=close1-MathAbs(high1-close1)*2.0;
            InitSignal(s,i,11,ORDER_TYPE_SELL,close1,sl,tp,55.0);
            if(tc>=3) AddScoreBonus(s,"Liq",10); if(tc>=5) AddScoreBonus(s,"Liq2",15);
            g_sym[i].liq[k].used=true; return s;
         }
      }
      else
      {
         if(low1<lvl && close1>lvl)
         {
            g_sym[i].liq[k].swept=true;
            double lw=MathMin(open1,close1)-low1, body=MathAbs(close1-open1);
            if(lw<body*0.5) continue;
            double sl=low1-st.atr*0.3, tp=close1+MathAbs(close1-low1)*2.0;
            InitSignal(s,i,11,ORDER_TYPE_BUY,close1,sl,tp,55.0);
            if(tc>=3) AddScoreBonus(s,"Liq",10); if(tc>=5) AddScoreBonus(s,"Liq2",15);
            g_sym[i].liq[k].used=true; return s;
         }
      }
   }
   return s;
}

// S13 Inside Bar Compression (closed-candle break, §2.9)
TradeSignal Strategy_S13(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   double pip=st.pip; int motherIdx=-1;
   for(int k=2;k<=5;k++) if(Hi(i,1)<=Hi(i,k)&&Lo(i,1)>=Lo(i,k)){ motherIdx=k; break; }
   if(motherIdx<0) return s;
   double mh=Hi(i,motherIdx), ml=Lo(i,motherIdx); int ic=motherIdx-1;
   double mr=(mh-ml)/pip; if(mr<15) return s;
   double closed=Cl(i,1);
   if(closed>mh+2*pip){ double sl=ml-st.atr*0.2, tp=mh+mr*pip; InitSignal(s,i,12,ORDER_TYPE_BUY,closed,sl,tp,45.0); AddScoreBonus(s,"Inside",ic*5.0); }
   else if(closed<ml-2*pip){ double sl=mh+st.atr*0.2, tp=ml-mr*pip; InitSignal(s,i,12,ORDER_TYPE_SELL,closed,sl,tp,45.0); AddScoreBonus(s,"Inside",ic*5.0); }
   return s;
}

// S14 Failed Breakout / Fakey
TradeSignal Strategy_S14(int i)
{
   TradeSignal s; s.valid=false; SymbolState st=g_sym[i];
   if(st.atr<=0) return s;
   double pip=st.pip, price=Cl(i,1);
   double resistance=NearestResistance(i,price), support=NearestSupport(i,price);
   double avgVol=AvgVolume(i,20);
   double r1h=Hi(i,2),r1l=Lo(i,2); long r1v=Vol(i,2);
   double r0c=Cl(i,1),r0o=Op(i,1);
   if(resistance>0 && r1h>resistance+2*pip && r0c<resistance-1*pip && r0c<r0o)
   { double sl=r1h+st.atr*0.3, sd=sl-r0c, tp=r0c-sd*2.0; InitSignal(s,i,13,ORDER_TYPE_SELL,r0c,sl,tp,50.0); if(avgVol>0&&r1v<avgVol*0.7) AddScoreBonus(s,"LowVolBreak",10); }
   else if(support>0 && r1l<support-2*pip && r0c>support+1*pip && r0c>r0o)
   { double sl=r1l-st.atr*0.3, sd=r0c-sl, tp=r0c+sd*2.0; InitSignal(s,i,13,ORDER_TYPE_BUY,r0c,sl,tp,50.0); if(avgVol>0&&r1v<avgVol*0.7) AddScoreBonus(s,"LowVolBreak",10); }
   return s;
}

TradeSignal RunStrategy(int s,int i)
{
   switch(s+1)
   {
      case 1: return Strategy_S1(i);  case 2: return Strategy_S2(i);  case 3: return Strategy_S3(i);
      case 4: return Strategy_S4(i);  case 5: return Strategy_S5(i);  case 6: return Strategy_S6(i);
      case 7: return Strategy_S7(i);  case 8: return Strategy_S8(i);  case 9: return Strategy_S9(i);
      case 10:return Strategy_S10(i); case 11:return Strategy_S11(i); case 12:return Strategy_S12(i);
      case 13:return Strategy_S13(i); case 14:return Strategy_S14(i);
   }
   TradeSignal none; none.valid=false; return none;
}

//==================================================================//
// SECTION 17 - RISK MANAGER                                        //
//==================================================================//
double PositionRiskPct(ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return 0.0;
   double entry=PositionGetDouble(POSITION_PRICE_OPEN), sl=PositionGetDouble(POSITION_SL), vol=PositionGetDouble(POSITION_VOLUME);
   string sym=PositionGetString(POSITION_SYMBOL);
   if(sl<=0.0) return InpRiskPerTrade;
   double tv=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE), ts=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_SIZE);
   if(ts<=0.0) return 0.0;
   double money=MathAbs(entry-sl)/ts*tv*vol;
   double bal=AccountInfoDouble(ACCOUNT_BALANCE);
   return (bal<=0?0:money/bal*100.0);
}

double PortfolioHeat()
{
   double h=0;
   for(int k=0;k<PositionsTotal();k++)
   {
      ulong t=PositionGetTicket(k); if(t==0) continue;
      if(PositionGetInteger(POSITION_MAGIC)<InpMagicBase) continue;
      h+=PositionRiskPct(t);
   }
   return h;
}

double CurrencyExposure(const string ccy)
{
   double net=0;
   for(int k=0;k<PositionsTotal();k++)
   {
      ulong t=PositionGetTicket(k); if(t==0) continue;
      if(PositionGetInteger(POSITION_MAGIC)<InpMagicBase) continue;
      string sym=PositionGetString(POSITION_SYMBOL);
      string b=SymbolInfoString(sym,SYMBOL_CURRENCY_BASE), q=SymbolInfoString(sym,SYMBOL_CURRENCY_PROFIT);
      int dir=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?+1:-1);
      double risk=PositionRiskPct(t);
      if(b==ccy) net+=dir*risk; if(q==ccy) net-=dir*risk;
   }
   return MathAbs(net);
}

int CountOpen(int s,int sym)
{
   long magic=MagicFor(s,sym); int n=0;
   for(int k=0;k<PositionsTotal();k++){ ulong t=PositionGetTicket(k); if(t==0) continue; if(PositionGetInteger(POSITION_MAGIC)==magic) n++; }
   return n;
}

double Correlation(int a,int b,int n=30)
{
   double sa=0,sb=0,saa=0,sbb=0,sab=0; int c=0;
   for(int k=1;k<=n;k++)
   {
      double ra=Cl(a,k)-Cl(a,k+1), rb=Cl(b,k)-Cl(b,k+1);
      sa+=ra; sb+=rb; saa+=ra*ra; sbb+=rb*rb; sab+=ra*rb; c++;
   }
   if(c<5) return 0;
   double cov=sab/c-(sa/c)*(sb/c), va=saa/c-(sa/c)*(sa/c), vb=sbb/c-(sb/c)*(sb/c);
   if(va<=0||vb<=0) return 0;
   return cov/MathSqrt(va*vb);
}

bool CorrelatedSameDirOpen(const TradeSignal &sig)
{
   if(!InpUseCorrelation) return false;
   int dirNew=(sig.type==ORDER_TYPE_BUY?+1:-1);
   for(int k=0;k<PositionsTotal();k++)
   {
      ulong t=PositionGetTicket(k); if(t==0) continue;
      if(PositionGetInteger(POSITION_MAGIC)<InpMagicBase) continue;
      string sym=PositionGetString(POSITION_SYMBOL); int other=-1;
      for(int j=0;j<g_symCount;j++) if(g_sym[j].name==sym){ other=j; break; }
      if(other<0||other==sig.symIdx) continue;
      int dirOpen=(PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?+1:-1);
      if(dirOpen!=dirNew) continue;
      if(Correlation(sig.symIdx,other)>0.7) return true;
   }
   return false;
}

double KellyMultiplier(int s)
{
   if(!InpUseKelly) return 1.0;
   if(g_stats[s].trades<InpDecayMinTrades) return 1.0;
   double wr=g_stats[s].winRate;
   double avgW=g_stats[s].avgWinPips, avgL=g_stats[s].avgLossPips;
   if(avgL<=0) return 1.0;
   double b=avgW/avgL;
   double kelly=wr-(1.0-wr)/b;           // full Kelly fraction
   double frac=kelly*InpKellyFraction;   // fractional Kelly
   // map to a multiplier around 1.0 (0.5..1.5), Shadow-safe (only when Active)
   if(g_perfMatrixState!=LAYER_ACTIVE) return 1.0;
   return Clamp(1.0+frac, 0.5, 1.5);
}

double ComputeFinalLot(const TradeSignal &sig)
{
   double bal=AccountInfoDouble(ACCOUNT_BALANCE);
   double riskMoney=bal*InpRiskPerTrade/100.0;
   double pv=PipValuePerLot(sig.symIdx);
   if(pv<=0 || sig.slPips<=0) return 0.0;
   double base=riskMoney/(sig.slPips*pv);
   double m=1.0;
   m*=KellyMultiplier(sig.stratIdx);
   if(g_equityCurveState==LAYER_ACTIVE)
   { double eq=AccountInfoDouble(ACCOUNT_EQUITY); if(g_dayStartEquity>0 && eq<g_dayStartEquity*0.99) m*=0.75; }
   double heat=PortfolioHeat(); if(heat>=InpHeatReduceLevel) m*=0.5;
   if(g_recoveryMode) m*=InpRecoveryRiskMult;   // #44
   m=Clamp(m,InpMinLotMult,InpMaxLotMult);
   double lot=NormalizeLot(g_sym[sig.symIdx].name, base*m);
   double realRisk=lot*sig.slPips*pv;
   if(realRisk>riskMoney*1.01) lot=NormalizeLot(g_sym[sig.symIdx].name, riskMoney/(sig.slPips*pv));
   return lot;
}

// forward decl
bool IsNewsBlocked(int symIdx);

bool ApproveTrade(const TradeSignal &sig,string &reason)
{
   if(g_paused){ reason="manually paused"; return false; }
   if(g_haltedDaily){ reason="daily loss limit"; return false; }
   if(g_haltedWeekly){ reason="weekly loss limit"; return false; }
   if(g_profitLocked){ reason="daily profit lock"; return false; }
   if(!g_healthOK){ reason="health watchdog"; return false; }
   if(g_accountConsecLosses>=InpMaxConsecLosses){ reason="consec-loss cooldown"; return false; }
   if(!TimeAllowsNewTrades()){ reason="time gate"; return false; }
   if(!g_sym[sig.symIdx].active){ reason="symbol paused (#43)"; return false; }
   if(g_stats[sig.stratIdx].demoted){ reason="strategy demoted (#38)"; return false; }
   if(sig.totalScore<InpMinScore){ reason="score below min"; return false; }
   if(sig.expectedValue<=0.0){ reason="non-positive EV"; return false; }
   if(sig.slPips<InpMinNetRRpips || sig.tpPips<InpMinNetRRpips){ reason="SL/TP too small"; return false; }
   if(CountOpen(sig.stratIdx,sig.symIdx)>0){ reason="already open (strat+sym)"; return false; }
   double heat=PortfolioHeat();
   if(heat>=InpPortfolioHeatMax){ reason=StringFormat("heat %.1f%%",heat); return false; }
   double addRisk=InpRiskPerTrade;
   if(CurrencyExposure(BaseCurrency(sig.symIdx))+addRisk>InpCurrencyExpMax){ reason="base ccy exposure cap"; return false; }
   if(CurrencyExposure(QuoteCurrency(sig.symIdx))+addRisk>InpCurrencyExpMax){ reason="quote ccy exposure cap"; return false; }
   if(CorrelatedSameDirOpen(sig)){ reason="correlated same-dir open"; return false; }
   if(SpreadPips(sig.symIdx)>InpMaxSpreadPips){ reason="spread too wide"; return false; }
   if(InpUseNewsFilter && IsNewsBlocked(sig.symIdx)){ reason="news window"; return false; }
   reason="approved"; return true;
}

//==================================================================//
// SECTION 18 - SHADOW: LOSS CLUSTER / ADAPTIVE SCORE               //
//==================================================================//
bool ShadowLossClusterBlocks(const TradeSignal &sig)
{
   StratStats st=g_stats[sig.stratIdx];
   if(st.recentLosses>=2 && st.lastLossTime>0)
   { if(TimeCurrent()<st.lastLossTime+4*3600) return true; }
   return false;
}
double AdaptiveMinScore(const TradeSignal &sig)
{
   double b=InpMinScore;
   if(g_stats[sig.stratIdx].recentLosses>=2) return b+5.0;
   return b;
}

//==================================================================//
// SECTION 19 - EXECUTION                                           //
//==================================================================//
string OVName(ulong ticket){ return EA_TAG+"_ov_"+IntegerToString((long)ticket); }
void StoreOriginalVolume(ulong ticket,double vol)
{
   if(g_isTester) return;
   GlobalVariableSet(OVName(ticket), vol);
}
double GetOriginalVolume(ulong ticket,double fallback)
{
   string n=OVName(ticket);
   if(GlobalVariableCheck(n)) return GlobalVariableGet(n);
   return fallback;
}

// stops-level guard (#41): ensure SL/TP respect broker minimum distance
void GuardStops(int i,int type,double price,double &sl,double &tp)
{
   double minDist=StopsLevelPrice(i);
   if(minDist<=0) return;
   if(type==ORDER_TYPE_BUY)
   {
      if(sl>0 && price-sl<minDist) sl=price-minDist;
      if(tp>0 && tp-price<minDist) tp=price+minDist;
   }
   else
   {
      if(sl>0 && sl-price<minDist) sl=price+minDist;
      if(tp>0 && price-tp<minDist) tp=price-minDist;
   }
   sl=NormalizePrice(i,sl); tp=NormalizePrice(i,tp);
}

void RecordSlippage(int i,double requested,double actual)
{
   int h=g_time.gmtHour; if(h<0||h>=24) return;
   double pip=SymbolPip(i); if(pip<=0) return;
   double slip=MathAbs(actual-requested)/pip;
   if(slip>20) slip=20; // clip outliers
   g_sym[i].slipSum[h]+=slip; g_sym[i].slipCount[h]++;
}

void QueueRetry(const TradeSignal &sig)
{
   int slot=sig.symIdx;
   g_pending[slot].active=true; g_pending[slot].sig=sig;
   g_pending[slot].attempts=0; g_pending[slot].nextTry=TimeCurrent();
}

bool TryExecute(const TradeSignal &sig)
{
   string sym=g_sym[sig.symIdx].name;
   double lot=ComputeFinalLot(sig);
   if(lot<=0){ Log("lot=0 skip "+sym); return true; }
   g_trade.SetExpertMagicNumber(MagicFor(sig.stratIdx,sig.symIdx));
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   g_trade.SetTypeFillingBySymbol(sym);
   g_trade.SetAsyncMode(InpUseAsyncExec);
   double ask=SymbolInfoDouble(sym,SYMBOL_ASK), bid=SymbolInfoDouble(sym,SYMBOL_BID);
   double price=(sig.type==ORDER_TYPE_BUY?ask:bid);
   double sl=sig.sl, tp=sig.tp;
   GuardStops(sig.symIdx,sig.type,price,sl,tp);
   string comment=StringFormat("%s_S%d",EA_TAG,sig.stratIdx+1);
   bool ok=(sig.type==ORDER_TYPE_BUY)?g_trade.Buy(lot,sym,price,sl,tp,comment):g_trade.Sell(lot,sym,price,sl,tp,comment);
   uint rc=g_trade.ResultRetcode();
   if(ok && (rc==TRADE_RETCODE_DONE||rc==TRADE_RETCODE_PLACED))
   {
      double fill=g_trade.ResultPrice(); if(fill>0) RecordSlippage(sig.symIdx,price,fill);
      LogAlways(StringFormat("EXEC %s %s S%d lot=%.2f @%.5f SL=%.5f TP=%.5f score=%.0f EV=%.1f [%s]",
         sym,(sig.type==ORDER_TYPE_BUY?"BUY":"SELL"),sig.stratIdx+1,lot,fill>0?fill:price,sl,tp,sig.totalScore,sig.expectedValue,sig.breakdown));
      CSVLogTrade(sig,lot);
      return true;
   }
   Log(StringFormat("exec failed %s rc=%u (%s)",sym,rc,g_trade.ResultRetcodeDescription()));
   return false;
}

void ProcessRetries()
{
   for(int i=0;i<g_symCount;i++)
   {
      if(!g_pending[i].active) continue;
      if(TimeCurrent()<g_pending[i].nextTry) continue;
      if(TryExecute(g_pending[i].sig)) g_pending[i].active=false;
      else
      {
         g_pending[i].attempts++;
         if(g_pending[i].attempts>=InpMaxRetries){ Log("give up "+g_sym[i].name); g_pending[i].active=false; }
         else g_pending[i].nextTry=TimeCurrent()+InpRetryDelaySec;
      }
   }
}

//==================================================================//
// SECTION 20 - EXIT MANAGER                                        //
//==================================================================//
void PartialProfile(int s,double &p1,double &p2,double &p3)
{
   int c=StrategyClass(s);
   if(c==0){ p1=0.60;p2=0.25;p3=0.15; }
   else if(c==1||c==2){ p1=0.35;p2=0.30;p3=0.35; }
   else { p1=0.50;p2=0.30;p3=0.20; }
}
double TrueBEPrice(int i,int type,double entry)
{
   double pip=SymbolPip(i), cost=(SpreadPips(i)+0.3)*pip;
   return (type==POSITION_TYPE_BUY?entry+cost:entry-cost);
}

void ManagePosition(ulong ticket)
{
   if(!PositionSelectByTicket(ticket)) return;
   long magic=PositionGetInteger(POSITION_MAGIC); if(magic<InpMagicBase) return;
   int s,i; DecodeMagic(magic,s,i); if(i<0||i>=g_symCount) return;
   string sym=PositionGetString(POSITION_SYMBOL);
   int type=(int)PositionGetInteger(POSITION_TYPE);
   double entry=PositionGetDouble(POSITION_PRICE_OPEN), sl=PositionGetDouble(POSITION_SL), tp=PositionGetDouble(POSITION_TP), vol=PositionGetDouble(POSITION_VOLUME);
   datetime openTime=(datetime)PositionGetInteger(POSITION_TIME);
   double pip=SymbolPip(i), atr=g_sym[i].atr;
   double bid=SymbolInfoDouble(sym,SYMBOL_BID), ask=SymbolInfoDouble(sym,SYMBOL_ASK);
   double cur=(type==POSITION_TYPE_BUY?bid:ask);
   double profitPips=(type==POSITION_TYPE_BUY?(cur-entry):(entry-cur))/pip;
   double slDist=MathAbs(entry-sl)/pip; if(slDist<=0) slDist=(atr>0?atr/pip:20.0);

   // P1 hard protection
   if(g_haltedDaily||g_haltedWeekly||g_time.fridayClose||g_time.weekend)
   { g_trade.PositionClose(ticket); Log(StringFormat("HARD-EXIT %s S%d",sym,s+1)); return; }

   // #42 stale-trade manager
   int barsHeld=(int)((TimeCurrent()-openTime)/3600);
   if(barsHeld>=InpStaleBars && MathAbs(profitPips)<slDist*InpStaleProgressR)
   { g_trade.PositionClose(ticket); Log(StringFormat("STALE-EXIT %s S%d",sym,s+1)); return; }

   // P2 proactive exit
   bool reversal=(type==POSITION_TYPE_BUY?IsBearishReversal(i,1):IsBullishReversal(i,1));
   bool momGone=(type==POSITION_TYPE_BUY?g_sym[i].diMinus>g_sym[i].diPlus:g_sym[i].diPlus>g_sym[i].diMinus);
   if(profitPips>slDist*0.5 && reversal && momGone)
   { g_trade.PositionClose(ticket); Log(StringFormat("PROACTIVE-EXIT %s S%d +%.1f",sym,s+1,profitPips)); return; }

   // P3 True BE
   double newSL=sl;
   if(profitPips>=slDist*1.0)
   {
      double be=TrueBEPrice(i,type,entry);
      if(type==POSITION_TYPE_BUY && sl<be) newSL=be;
      if(type==POSITION_TYPE_SELL && (sl>be||sl==0.0)) newSL=be;
   }
   // P4 unified trailing
   double startR=(g_sym[i].regime==REGIME_STRONG_TREND?0.8:1.2);
   double newTP=tp;
   if(profitPips>=slDist*startR)
   {
      double tmult=(g_sym[i].adx>=30?1.5:1.0), tdist=atr*tmult;
      double cand=(type==POSITION_TYPE_BUY?cur-tdist:cur+tdist);
      if(type==POSITION_TYPE_BUY && cand>newSL) newSL=cand;
      if(type==POSITION_TYPE_SELL && (cand<newSL||newSL==0.0)) newSL=cand;
      if(type==POSITION_TYPE_BUY && g_sym[i].diPlus>g_sym[i].diMinus) newTP=MathMax(tp,cur+atr*2.0);
      if(type==POSITION_TYPE_SELL && g_sym[i].diMinus>g_sym[i].diPlus) newTP=(tp==0.0?cur-atr*2.0:MathMin(tp,cur-atr*2.0));
   }
   GuardStops(i,type,cur,newSL,newTP);
   if(MathAbs(newSL-sl)>pip*0.5 || MathAbs(newTP-tp)>pip*0.5)
      if(g_trade.PositionModify(ticket,newSL,newTP))
         Log(StringFormat("TRAIL %s S%d SL=%.5f TP=%.5f (+%.1f)",sym,s+1,newSL,newTP,profitPips));
}

void ManageAllPositions()
{
   if(!InpUseExitManager) return;
   for(int k=PositionsTotal()-1;k>=0;k--){ ulong t=PositionGetTicket(k); if(t==0) continue; ManagePosition(t); }
}

//==================================================================//
// SECTION 21 - STATS / SHADOW LAYERS                               //
//==================================================================//
void RecomputeStratStats(int s)
{
   if(g_stats[s].trades>0) g_stats[s].winRate=(double)g_stats[s].wins/g_stats[s].trades;
   if(g_stats[s].wins>0) g_stats[s].avgWinPips=g_stats[s].sumWinPips/g_stats[s].wins;
   int losses=g_stats[s].trades-g_stats[s].wins;
   if(losses>0) g_stats[s].avgLossPips=g_stats[s].sumLossPips/losses;
}

double RollingPF(int s)
{
   double gp=0,gl=0;
   for(int k=0;k<g_stats[s].r30count;k++){ double r=g_stats[s].last30R[k]; if(r>=0) gp+=r; else gl+=-r; }
   if(gl<=0) return (gp>0?99.0:1.0);
   return gp/gl;
}

void OnDealClosed(int s,double profit,double pips,double rMultiple)
{
   if(s<0||s>=NUM_STRATEGIES) return;
   g_stats[s].trades++;
   if(profit>=0){ g_stats[s].wins++; g_stats[s].sumWinPips+=MathAbs(pips); g_stats[s].recentLosses=0; g_accountConsecLosses=0; }
   else { g_stats[s].sumLossPips+=MathAbs(pips); g_stats[s].recentLosses++; g_stats[s].lastLossTime=TimeCurrent(); g_accountConsecLosses++; }
   // rolling R window
   g_stats[s].last30R[g_stats[s].r30idx]=rMultiple;
   g_stats[s].r30idx=(g_stats[s].r30idx+1)%R30;
   if(g_stats[s].r30count<R30) g_stats[s].r30count++;
   RecomputeStratStats(s);
   // #38 decay monitor
   if(InpDecayMonitor && g_stats[s].r30count>=InpDecayMinTrades)
   {
      double pf=RollingPF(s);
      if(pf<InpDecayPFFloor && !g_stats[s].demoted){ g_stats[s].demoted=true; LogAlways(StringFormat("DECAY: S%d demoted (rolling PF %.2f < %.2f)",s+1,pf,InpDecayPFFloor)); }
      else if(pf>InpDecayPFFloor+0.2 && g_stats[s].demoted){ g_stats[s].demoted=false; LogAlways(StringFormat("DECAY: S%d re-promoted (PF %.2f)",s+1,pf)); }
   }
   if(InpShadowMode && g_stats[s].trades==InpPromoteMinSample)
      LogAlways(StringFormat("SHADOW: S%d reached %d trades (WR=%.2f) - promotion candidate",s+1,g_stats[s].trades,g_stats[s].winRate));
}

//==================================================================//
// SECTION 22 - PERSISTENCE                                         //
//==================================================================//
string StateFileName(){ return "STB61_state.bin"; }
void SaveState()
{
   if(g_isTester) return;
   int h=FileOpen(StateFileName(),FILE_WRITE|FILE_BIN); if(h==INVALID_HANDLE) return;
   FileWriteInteger(h,STATE_FILE_MAGIC,INT_VALUE);
   FileWriteInteger(h,STATE_FILE_VERSION,INT_VALUE);
   FileWriteInteger(h,g_symCount,INT_VALUE);
   for(int i=0;i<g_symCount;i++) FileWriteString(h,g_sym[i].name,16);
   for(int s=0;s<NUM_STRATEGIES;s++)
   {
      FileWriteInteger(h,g_stats[s].trades,INT_VALUE);
      FileWriteInteger(h,g_stats[s].wins,INT_VALUE);
      FileWriteDouble(h,g_stats[s].sumWinPips);
      FileWriteDouble(h,g_stats[s].sumLossPips);
   }
   FileClose(h);
}
void LoadState()
{
   if(g_isTester) return;
   if(!FileIsExist(StateFileName())) return;
   int h=FileOpen(StateFileName(),FILE_READ|FILE_BIN); if(h==INVALID_HANDLE) return;
   int mg=(int)FileReadInteger(h,INT_VALUE), ver=(int)FileReadInteger(h,INT_VALUE);
   if(mg!=STATE_FILE_MAGIC||ver!=STATE_FILE_VERSION){ FileClose(h); return; }
   int cnt=(int)FileReadInteger(h,INT_VALUE);
   for(int i=0;i<cnt;i++) FileReadString(h,16);
   for(int s=0;s<NUM_STRATEGIES;s++)
   {
      g_stats[s].trades=(int)FileReadInteger(h,INT_VALUE);
      g_stats[s].wins=(int)FileReadInteger(h,INT_VALUE);
      g_stats[s].sumWinPips=FileReadDouble(h);
      g_stats[s].sumLossPips=FileReadDouble(h);
      RecomputeStratStats(s);
   }
   FileClose(h);
   LogAlways("state loaded");
}

//==================================================================//
// SECTION 23 - KILL SWITCH / PROFIT LOCK / RECOVERY / HEALTH       //
//==================================================================//
void UpdateKillSwitch()
{
   MqlDateTime d; TimeToStruct(TimeCurrent(),d);
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_curDayOfYear!=d.day_of_year)
   { g_curDayOfYear=d.day_of_year; g_dayStartEquity=eq; g_dayPeakEquity=eq; g_haltedDaily=false; g_profitLocked=false; Log("new day baseline"); }
   int wk=d.day_of_year/7;
   if(g_curWeek!=wk){ g_curWeek=wk; g_weekStartEquity=eq; g_haltedWeekly=false; }
   if(eq>g_dayPeakEquity) g_dayPeakEquity=eq;
   if(eq>g_equityPeak) g_equityPeak=eq;

   if(g_dayStartEquity>0)
   {
      double dp=(eq-g_dayStartEquity)/g_dayStartEquity*100.0;
      if(dp<=-InpDailyLossLimit && !g_haltedDaily){ g_haltedDaily=true; LogAlways(StringFormat("KILL: daily %.2f%% halt",dp)); }
      // #34 daily profit lock
      double peakGain=(g_dayPeakEquity-g_dayStartEquity)/g_dayStartEquity*100.0;
      double giveback=(g_dayPeakEquity-eq)/g_dayStartEquity*100.0;
      if(peakGain>=InpDailyProfitTarget && giveback>=InpDailyGiveback && !g_profitLocked)
      { g_profitLocked=true; LogAlways(StringFormat("PROFIT-LOCK: peak +%.2f%%, gave back %.2f%% - new trades halted",peakGain,giveback)); }
   }
   if(g_weekStartEquity>0)
   {
      double wp=(eq-g_weekStartEquity)/g_weekStartEquity*100.0;
      if(wp<=-InpWeeklyLossLimit && !g_haltedWeekly){ g_haltedWeekly=true; LogAlways(StringFormat("KILL: weekly %.2f%% halt + review",wp)); }
   }
   // #44 recovery ladder
   if(g_equityPeak>0)
   {
      double dd=(g_equityPeak-eq)/g_equityPeak*100.0;
      bool wasRec=g_recoveryMode;
      g_recoveryMode=(dd>=InpRecoveryDDTrigger);
      if(g_recoveryMode && !wasRec) LogAlways(StringFormat("RECOVERY mode ON (DD %.2f%%)",dd));
      if(!g_recoveryMode && wasRec) LogAlways("RECOVERY mode OFF");
   }
}

void UpdateHealthWatchdog()
{
   if(!InpUseHealthWatchdog){ g_healthOK=true; return; }
   bool ok=true;
   if(!TerminalInfoInteger(TERMINAL_CONNECTED)) ok=false;
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED)) ok=false;
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED)) ok=false;
   // stale data check on first symbol
   if(g_symCount>0)
   {
      datetime bt=Tm(0,0);
      if(bt==0) ok=false;
   }
   if(ok!=g_healthOK) LogAlways(StringFormat("HEALTH: %s",ok?"OK":"DEGRADED -> new trades paused"));
   g_healthOK=ok;
}

// #43 symbol auto-pause refresh
void UpdateSymbolPause()
{
   for(int i=0;i<g_symCount;i++)
   {
      g_sym[i].rollPnl*=0.999;  // slow decay
      if(g_sym[i].pauseUntil>0 && TimeCurrent()>=g_sym[i].pauseUntil){ g_sym[i].pauseUntil=0; g_sym[i].active=true; }
   }
}

//==================================================================//
// SECTION 24 - NEWS FILTER (#33)                                   //
//==================================================================//
bool IsNewsBlocked(int symIdx)
{
   if(g_isTester) return false;          // calendar not reliable in tester
   string base=SymbolInfoString(g_sym[symIdx].name,SYMBOL_CURRENCY_BASE);
   string quote=SymbolInfoString(g_sym[symIdx].name,SYMBOL_CURRENCY_PROFIT);
   datetime from=TimeTradeServer()-InpNewsAfterMin*60;
   datetime to  =TimeTradeServer()+InpNewsBeforeMin*60;
   MqlCalendarValue values[];
   int n=CalendarValueHistory(values,from,to);
   if(n<=0) return false;
   for(int k=0;k<n;k++)
   {
      MqlCalendarEvent ev;
      if(!CalendarEventById(values[k].event_id,ev)) continue;
      if(ev.importance!=CALENDAR_IMPORTANCE_HIGH) continue;
      MqlCalendarCountry ctry;
      if(!CalendarCountryById(ev.country_id,ctry)) continue;
      if(ctry.currency==base || ctry.currency==quote) return true;
   }
   return false;
}

//==================================================================//
// SECTION 25 - TELEGRAM (#37 commands + inline buttons)            //
//==================================================================//
bool TG_Enabled(){ return (InpUseTelegram && !g_isTester && InpTelegramToken!="" && InpTelegramChatID!=""); }

void TG_Post(const string method,const string payload)
{
   if(!TG_Enabled()) return;
   string url="https://api.telegram.org/bot"+InpTelegramToken+"/"+method;
   uchar post[]; StringToCharArray(payload,post,0,StringLen(payload),CP_UTF8);
   uchar result[]; string rh; string headers="Content-Type: application/json\r\n";
   int r=WebRequest("POST",url,headers,1500,post,result,rh);
   if(r==-1) Log("Telegram WebRequest failed - add api.telegram.org to allowed URLs");
}

string JsonEscape(const string s)
{
   string o=s;
   StringReplace(o,"\\","\\\\"); StringReplace(o,"\"","\\\""); StringReplace(o,"\n","\\n");
   return o;
}

void TG_Send(const string text)
{
   if(!TG_Enabled()) return;
   string payload="{\"chat_id\":\""+InpTelegramChatID+"\",\"text\":\""+JsonEscape(text)+"\",\"parse_mode\":\"HTML\"}";
   TG_Post("sendMessage",payload);
}

void TG_SendButtons(const string text)
{
   if(!TG_Enabled()) return;
   string kb="{\"inline_keyboard\":[[{\"text\":\"Status\",\"callback_data\":\"status\"},{\"text\":\"Pause\",\"callback_data\":\"pause\"},{\"text\":\"Resume\",\"callback_data\":\"resume\"}],[{\"text\":\"Today\",\"callback_data\":\"today\"},{\"text\":\"Close All?\",\"callback_data\":\"closeall_confirm\"}]]}";
   string payload="{\"chat_id\":\""+InpTelegramChatID+"\",\"text\":\""+JsonEscape(text)+"\",\"parse_mode\":\"HTML\",\"reply_markup\":"+kb+"}";
   TG_Post("sendMessage",payload);
}

string StatusText()
{
   double bal=AccountInfoDouble(ACCOUNT_BALANCE), eq=AccountInfoDouble(ACCOUNT_EQUITY);
   double dp=(g_dayStartEquity>0?(eq-g_dayStartEquity)/g_dayStartEquity*100.0:0);
   int openN=0; for(int k=0;k<PositionsTotal();k++){ ulong t=PositionGetTicket(k); if(t!=0 && PositionGetInteger(POSITION_MAGIC)>=InpMagicBase) openN++; }
   return StringFormat("STB61\nBal %.2f | Eq %.2f | Day %.2f%%\nHeat %.1f%% | Open %d\nPaused:%s Halt:%s Lock:%s Recov:%s",
      bal,eq,dp,PortfolioHeat(),openN,(g_paused?"Y":"N"),(g_haltedDaily?"Y":"N"),(g_profitLocked?"Y":"N"),(g_recoveryMode?"Y":"N"));
}

void CloseAllBot()
{
   for(int k=PositionsTotal()-1;k>=0;k--){ ulong t=PositionGetTicket(k); if(t==0) continue; if(PositionGetInteger(POSITION_MAGIC)>=InpMagicBase) g_trade.PositionClose(t); }
}

void HandleCommand(const string cmd)
{
   string c=cmd; StringToLower(c);
   if(StringFind(c,"status")>=0)      TG_SendButtons(StatusText());
   else if(StringFind(c,"pause")>=0)  { g_paused=true;  TG_Send("Paused."); }
   else if(StringFind(c,"resume")>=0) { g_paused=false; TG_Send("Resumed."); }
   else if(StringFind(c,"today")>=0)  TG_Send(StatusText());
   else if(StringFind(c,"closeall_confirm")>=0) TG_Send("Send /closeall to confirm closing ALL bot trades.");
   else if(StringFind(c,"closeall")>=0){ CloseAllBot(); TG_Send("All bot trades closed."); }
}

// Minimal getUpdates poll + parse (text commands + callback_data)
void TG_CheckCommands()
{
   if(!TG_Enabled()) return;
   string url="https://api.telegram.org/bot"+InpTelegramToken+"/getUpdates?offset="+IntegerToString(g_tgLastUpdateId+1)+"&timeout=0";
   uchar post[]; uchar result[]; string rh;
   int r=WebRequest("GET",url,"",1500,post,result,rh);
   if(r!=200) return;
   string body=CharArrayToString(result,0,-1,CP_UTF8);
   // naive scan for update_id / text / callback data
   int pos=0;
   while(true)
   {
      int up=StringFind(body,"\"update_id\":",pos); if(up<0) break;
      int idStart=up+12; int idEnd=StringFind(body,",",idStart);
      if(idEnd<0) break;
      long uid=(long)StringToInteger(StringSubstr(body,idStart,idEnd-idStart));
      if(uid>g_tgLastUpdateId) g_tgLastUpdateId=uid;
      int nextUp=StringFind(body,"\"update_id\":",idEnd);
      int segEnd=(nextUp<0?StringLen(body):nextUp);
      string seg=StringSubstr(body,up,segEnd-up);
      // callback_data
      int cb=StringFind(seg,"\"callback_data\":\"");
      if(cb>=0){ int st=cb+17; int en=StringFind(seg,"\"",st); if(en>st) HandleCommand(StringSubstr(seg,st,en-st)); }
      else
      {
         int tx=StringFind(seg,"\"text\":\"");
         if(tx>=0){ int st=tx+8; int en=StringFind(seg,"\"",st); if(en>st) HandleCommand(StringSubstr(seg,st,en-st)); }
      }
      pos=segEnd;
   }
}

//==================================================================//
// SECTION 26 - DASHBOARD (on-chart panel)                          //
//==================================================================//
#define DASH_X    10
#define DASH_Y    24
#define DASH_W    420
#define DASH_ROW  16

void DashBG(int rows)
{
   string n=EA_TAG+"_dbg";
   if(ObjectFind(0,n)<0)
   {
      ObjectCreate(0,n,OBJ_RECTANGLE_LABEL,0,0,0);
      ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,n,OBJPROP_XDISTANCE,DASH_X-6);
      ObjectSetInteger(0,n,OBJPROP_YDISTANCE,DASH_Y-8);
      ObjectSetInteger(0,n,OBJPROP_BGCOLOR,C'18,20,28');
      ObjectSetInteger(0,n,OBJPROP_BORDER_TYPE,BORDER_FLAT);
      ObjectSetInteger(0,n,OBJPROP_COLOR,clrSlateGray);
      ObjectSetInteger(0,n,OBJPROP_BACK,false);
      ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);
   }
   ObjectSetInteger(0,n,OBJPROP_XSIZE,DASH_W);
   ObjectSetInteger(0,n,OBJPROP_YSIZE,rows*DASH_ROW+18);
}

void DashTxt(int row,int colpx,const string text,color clr,int fsize=9)
{
   string n=EA_TAG+"_d_"+IntegerToString(row)+"_"+IntegerToString(colpx);
   if(ObjectFind(0,n)<0)
   {
      ObjectCreate(0,n,OBJ_LABEL,0,0,0);
      ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);
      ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);
      ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);
      ObjectSetString(0,n,OBJPROP_FONT,"Consolas");
   }
   ObjectSetInteger(0,n,OBJPROP_XDISTANCE,DASH_X+colpx);
   ObjectSetInteger(0,n,OBJPROP_YDISTANCE,DASH_Y+row*DASH_ROW);
   ObjectSetInteger(0,n,OBJPROP_FONTSIZE,fsize);
   ObjectSetInteger(0,n,OBJPROP_COLOR,clr);
   ObjectSetString(0,n,OBJPROP_TEXT,text);
}

string SessionName()
{
   if(g_time.sessionIdx==1) return "London";
   if(g_time.sessionIdx==2) return "NewYork";
   return "Asian";
}

void UpdateDashboard()
{
   if(!InpUseDashboard || g_isTester) return;
   DashBG(8);

   double bal=AccountInfoDouble(ACCOUNT_BALANCE), eq=AccountInfoDouble(ACCOUNT_EQUITY);
   double dp=(g_dayStartEquity>0?(eq-g_dayStartEquity)/g_dayStartEquity*100.0:0);
   double heat=PortfolioHeat();
   color cP=(dp>=0?clrLime:clrTomato);
   color cH=(heat>=InpPortfolioHeatMax?clrTomato:(heat>=InpHeatReduceLevel?clrGold:clrLime));

   int openByStrat[NUM_STRATEGIES]; ArrayInitialize(openByStrat,0);
   int totalOpen=0;
   for(int k=0;k<PositionsTotal();k++)
   {
      ulong t=PositionGetTicket(k); if(t==0) continue;
      long mg=PositionGetInteger(POSITION_MAGIC); if(mg<InpMagicBase) continue;
      int ss,ii; DecodeMagic(mg,ss,ii);
      if(ss>=0 && ss<NUM_STRATEGIES) openByStrat[ss]++;
      totalOpen++;
   }

   DashTxt(0,0,"Speed Trader Bot v6.1",clrAqua,11);
   DashTxt(0,250,"Symbols: "+IntegerToString(g_symCount),clrSilver,9);

   DashTxt(1,0,StringFormat("Balance: %.2f",bal),clrWhite);
   DashTxt(1,200,StringFormat("Equity: %.2f",eq),clrWhite);

   DashTxt(2,0,StringFormat("Day P/L: %+.2f%%",dp),cP);
   DashTxt(2,200,StringFormat("Heat: %.1f%%",heat),cH);

   DashTxt(3,0,StringFormat("Open: %d",totalOpen),clrWhite);
   DashTxt(3,120,"Session: "+SessionName(),clrWhite);
   DashTxt(3,290,"GMT "+TimeToString(g_time.gmt,TIME_MINUTES),clrSilver);

   DashTxt(4,0,  "Halt:"+(g_haltedDaily?"Y":"-"),   g_haltedDaily?clrTomato:clrGray);
   DashTxt(4,70, "Lock:"+(g_profitLocked?"Y":"-"),  g_profitLocked?clrGold:clrGray);
   DashTxt(4,140,"Recov:"+(g_recoveryMode?"Y":"-"), g_recoveryMode?clrGold:clrGray);
   DashTxt(4,230,"Health:"+(g_healthOK?"OK":"BAD"), g_healthOK?clrLime:clrTomato);
   DashTxt(4,330,"Pause:"+(g_paused?"Y":"-"),       g_paused?clrGold:clrGray);

   DashTxt(5,0,"Strategies  (open | WR%):",clrSilver);

   for(int s=0;s<NUM_STRATEGIES;s++)
   {
      int rr=6+s/7;
      int cc=(s%7)*60;
      color clr2;
      if(!StrategyEnabled(s))            clr2=clrDimGray;
      else if(g_stats[s].demoted)        clr2=clrTomato;
      else if(g_stats[s].trades==0)      clr2=clrGray;
      else if(g_stats[s].winRate>=0.55)  clr2=clrLime;
      else if(g_stats[s].winRate>=0.45)  clr2=clrGold;
      else                               clr2=clrOrange;
      string cell=StringFormat("S%d %d|%.0f",s+1,openByStrat[s],g_stats[s].winRate*100.0);
      DashTxt(rr,cc,cell,clr2,8);
   }
}

void ClearDashboard(){ ObjectsDeleteAll(0,EA_TAG+"_d"); }

//==================================================================//
// SECTION 27 - MAIN PIPELINE                                       //
//==================================================================//
void RefreshSymbol(int i)
{
   if(!UpdateIndicators(i)) return;
   UpdateFib(i); UpdateVWAP(i); UpdateORB(i);
   datetime bt=Tm(i,1);
   if(bt!=g_sym[i].lastH1Bar){ g_sym[i].lastH1Bar=bt; UpdateLiquidityLevels(i); }
}

bool FlashSpike(int i)
{
   // #40 abnormal last-bar move using ATR from before the bar
   double rng=Hi(i,1)-Lo(i,1);
   double atrPrev=g_sym[i].atrPrev; if(atrPrev<=0) atrPrev=g_sym[i].atr;
   return (atrPrev>0 && rng>InpFlashSpikeATR*atrPrev);
}

void ProcessSymbol(int i)
{
   if(!g_sym[i].ready || !g_sym[i].active) return;
   if(FlashSpike(i)) return;             // #40

   TradeSignal best; best.valid=false; best.combinedPriority=-DBL_MAX;
   for(int s=0;s<NUM_STRATEGIES;s++)
   {
      if(!StrategyEnabled(s)) continue;
      if(g_stats[s].demoted) continue;   // #38
      if(!StateAllows(i,s)) continue;    // 8-state gating
      TradeSignal sig=RunStrategy(s,i);
      if(!sig.valid) continue;
      if(sig.type==ORDER_TYPE_BUY && (sig.sl>=sig.entry||sig.tp<=sig.entry)) continue;
      if(sig.type==ORDER_TYPE_SELL && (sig.sl<=sig.entry||sig.tp>=sig.entry)) continue;
      if(sig.slPips<InpMinNetRRpips || sig.tpPips<InpMinNetRRpips) continue;
      ScoreAndRank(sig);
      double minScore=AdaptiveMinScore(sig);
      if(g_adaptiveScoreState==LAYER_ACTIVE){ if(sig.totalScore<minScore) continue; }
      else if(InpShadowMode && sig.totalScore<minScore) Log(StringFormat("SHADOW #25 would block S%d %s",s+1,g_sym[i].name));
      if(ShadowLossClusterBlocks(sig))
      { if(g_lossClusterState==LAYER_ACTIVE) continue; else if(InpShadowMode) Log(StringFormat("SHADOW #8 would block S%d %s",s+1,g_sym[i].name)); }
      if(sig.combinedPriority>best.combinedPriority) best=sig;
   }
   if(!best.valid) return;
   string reason;
   if(!ApproveTrade(best,reason)){ Log(StringFormat("BLOCKED S%d %s: %s",best.stratIdx+1,g_sym[best.symIdx].name,reason)); return; }
   QueueRetry(best);
}

//==================================================================//
// SECTION 28 - EVENT HANDLERS                                      //
//==================================================================//
bool ParseSymbols()
{
   string parts[]; int n=StringSplit(InpSymbols,',',parts);
   if(n<=0){ LogAlways("no symbols"); return false; }
   ArrayResize(g_sym,0); g_symCount=0;
   for(int i=0;i<n && g_symCount<MAX_SYMBOLS;i++)
   {
      string name=parts[i]; StringTrimLeft(name); StringTrimRight(name);
      if(name=="") continue;
      if(!SymbolSelect(name,true)){ LogAlways("unavailable: "+name); continue; }
      ArrayResize(g_sym,g_symCount+1);
      SymbolState st;
      st.name=name; st.digits=(int)SymbolInfoInteger(name,SYMBOL_DIGITS);
      st.point=SymbolInfoDouble(name,SYMBOL_POINT);
      st.pip=(st.digits==3||st.digits==5)?st.point*10.0:st.point;
      st.tickValue=SymbolInfoDouble(name,SYMBOL_TRADE_TICK_VALUE);
      st.tickSize=SymbolInfoDouble(name,SYMBOL_TRADE_TICK_SIZE);
      st.ready=false; st.active=true; st.pauseUntil=0; st.rollPnl=0;
      st.bbWidthCount=0; st.liqCount=0; st.orbDay=-1; st.lastH1Bar=0; st.lastM30Bar=0;
      for(int h=0;h<24;h++){ st.slipSum[h]=0; st.slipCount[h]=0; }
      g_sym[g_symCount]=st;
      if(!CreateHandles(g_symCount)){ LogAlways("handle fail "+name); return false; }
      g_symCount++;
   }
   LogAlways(StringFormat("initialized %d symbols",g_symCount));
   return (g_symCount>0);
}

void ReconcileState()   // #39
{
   // adopt orphan open positions: nothing to rebuild beyond stats (kept persisted);
   // ensure original-volume GVs exist for any open bot position
   for(int k=0;k<PositionsTotal();k++)
   {
      ulong t=PositionGetTicket(k); if(t==0) continue;
      if(PositionGetInteger(POSITION_MAGIC)<InpMagicBase) continue;
      string gv=OVName(t);
      if(!GlobalVariableCheck(gv)) GlobalVariableSet(gv,PositionGetDouble(POSITION_VOLUME));
   }
   LogAlways("state reconciled with open positions");
}

int OnInit()
{
   g_isTester=(bool)MQLInfoInteger(MQL_TESTER);
   ENUM_LAYER_STATE def=(InpShadowMode?LAYER_SHADOW:LAYER_ACTIVE);
   g_perfMatrixState=def; g_lossClusterState=def; g_adaptiveScoreState=def; g_equityCurveState=def; g_dayHourState=def;
   for(int s=0;s<NUM_STRATEGIES;s++)
   {
      g_stats[s].trades=0; g_stats[s].wins=0; g_stats[s].sumWinPips=0; g_stats[s].sumLossPips=0;
      g_stats[s].winRate=0; g_stats[s].avgWinPips=0; g_stats[s].avgLossPips=0;
      g_stats[s].recentLosses=0; g_stats[s].lastLossTime=0; g_stats[s].r30idx=0; g_stats[s].r30count=0; g_stats[s].demoted=false;
      for(int k=0;k<R30;k++) g_stats[s].last30R[k]=0;
   }
   for(int i=0;i<MAX_SYMBOLS;i++) g_pending[i].active=false;
   if(!ParseSymbols()) return INIT_FAILED;
   LoadState();
   ReconcileState();
   OpenCSV();
   double eq=AccountInfoDouble(ACCOUNT_EQUITY);
   g_dayStartEquity=eq; g_weekStartEquity=eq; g_dayPeakEquity=eq; g_equityPeak=eq;
   EventSetTimer(InpTimerSeconds);
   LogAlways(StringFormat("Speed Trader Bot v6.1 init | tester=%s | shadow=%s | symbols=%d",(g_isTester?"yes":"no"),(InpShadowMode?"ON":"OFF"),g_symCount));
   if(TG_Enabled()) TG_SendButtons("STB61 started.\n"+StatusText());
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   SaveState();
   for(int i=0;i<g_symCount;i++) ReleaseHandles(i);
   if(g_csvHandle!=INVALID_HANDLE){ FileClose(g_csvHandle); g_csvHandle=INVALID_HANDLE; }
   ClearDashboard();
   LogAlways("deinit reason "+IntegerToString(reason));
}

void OnTimer()
{
   UpdateTimeEngine();
   UpdateKillSwitch();
   UpdateHealthWatchdog();
   UpdateSymbolPause();

   bool canTrade=(TimeAllowsNewTrades() && !g_haltedDaily && !g_haltedWeekly && !g_profitLocked && !g_paused && g_healthOK);

   // Heavy work (indicators + strategies) runs ONLY on a new M30 bar per symbol.
   // This is the main performance lever: strategies act on closed bars, so there
   // is no need to recompute every timer tick. Big speedup in the tester.
   for(int i=0;i<g_symCount;i++)
   {
      datetime cur=iTime(g_sym[i].name,PERIOD_M30,0);
      if(cur==0) continue;
      if(cur==g_sym[i].lastM30Bar) continue;
      g_sym[i].lastM30Bar=cur;
      RefreshSymbol(i);
      if(canTrade) ProcessSymbol(i);
   }

   ProcessRetries();
   UpdateDashboard();
   static datetime lastTG=0;
   if(TG_Enabled() && TimeCurrent()-lastTG>=30){ TG_CheckCommands(); lastTG=TimeCurrent(); }
}

void OnTick()
{
   static datetime last=0;
   datetime now=TimeCurrent();
   if(now==last) return;        // throttle position management to ~once/second
   last=now;
   ManageAllPositions();
}

void OnTradeTransaction(const MqlTradeTransaction &trans,const MqlTradeRequest &request,const MqlTradeResult &result)
{
   if(trans.type!=TRADE_TRANSACTION_DEAL_ADD) return;
   ulong deal=trans.deal; if(deal==0) return;
   if(!HistoryDealSelect(deal)) return;
   long entry=HistoryDealGetInteger(deal,DEAL_ENTRY);
   if(entry!=DEAL_ENTRY_OUT && entry!=DEAL_ENTRY_INOUT) return;
   long magic=HistoryDealGetInteger(deal,DEAL_MAGIC);
   if(magic<InpMagicBase) return;
   int s,i; DecodeMagic(magic,s,i);
   double profit=HistoryDealGetDouble(deal,DEAL_PROFIT)+HistoryDealGetDouble(deal,DEAL_SWAP)+HistoryDealGetDouble(deal,DEAL_COMMISSION);
   double pips=0, rMultiple=0;
   if(i>=0 && i<g_symCount)
   {
      double pv=PipValuePerLot(i), vol=HistoryDealGetDouble(deal,DEAL_VOLUME);
      if(pv>0 && vol>0){ pips=profit/(pv*vol); }
      // approximate R multiple from pips vs a nominal SL (avgLoss) if available
      double denom=(g_stats[s].avgLossPips>0?g_stats[s].avgLossPips:20.0);
      rMultiple=pips/denom;
      g_sym[i].rollPnl+=profit;
      // #43 symbol auto-pause on rolling drawdown
      double bal=AccountInfoDouble(ACCOUNT_BALANCE);
      if(bal>0 && g_sym[i].rollPnl < -(InpSymbolPauseDD/100.0*bal))
      { g_sym[i].active=false; g_sym[i].pauseUntil=TimeCurrent()+InpSymbolPauseHours*3600; g_sym[i].rollPnl=0; LogAlways(StringFormat("SYMBOL-PAUSE %s for %dh",g_sym[i].name,InpSymbolPauseHours)); }
   }
   OnDealClosed(s,profit,pips,rMultiple);
   string gv=OVName(trans.position);
   if(GlobalVariableCheck(gv)) GlobalVariableDel(gv);
}

double OnTester()
{
   double profit=TesterStatistics(STAT_PROFIT);
   double dd=TesterStatistics(STAT_EQUITY_DDREL_PERCENT);
   double pf=TesterStatistics(STAT_PROFIT_FACTOR);
   if(dd<=0) dd=0.01;
   return (profit/dd)*(pf>0?pf:0.1);
}
//+------------------------------------------------------------------+
