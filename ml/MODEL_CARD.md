# Shadow risk model: model card

**Model:** logistic regression, `paysim-logreg-2026-10-18` (weights in `data/risk_model.json`)
**Mode:** shadow only. The model scores each transaction after the rules have decided. The score is logged (`shadow_risk_score` in `/logs`) and returned by `/analyze`, but it **never changes a decision**. `tests/test_risk_model.py` checks that decisions stay identical when the model works, crashes, or flags everything.

## Training data

PaySim, *Synthetic Financial Datasets For Fraud Detection*, by E. A. Lopez-Rojas, A. Elmir and S. Axelsson (2016). Source: <https://www.kaggle.com/datasets/ealaxi/paysim1>. Licence: **CC BY-SA 4.0**.

PaySim is simulated mobile-money data, generated from one month of real transaction logs from a mobile-money service in an African country. The data is not stored in this repo. `ml/train_risk_model.py` downloads it.

- **Used:** the 2,684,404 `PAYMENT` and `TRANSFER` rows, because DialectGuard has only bill payments and transfers. 4,097 of them are fraud, all transfers.
- **Split by time:** the first 70% of rows (by hour) train the model. The next 15% choose the alert threshold. The last 15% (402,661 rows, 2,003 fraud) are the test set.

## Features

The same `risk_model.features()` code computes the features in training and in the live app. Raw amounts are not used, because PaySim's currency and amount scale don't match RM bills. Every feature is scale-free:

| Feature | Meaning |
|---|---|
| `is_transfer` | Transfer rather than bill payment |
| `log_amount_to_balance` | Amount as a share of the balance before payment (capped) |
| `empties_balance` | Sends 99–100% of the balance |
| `night_hour` | Between midnight and 6am, Malaysia time in the app |

## Results on the test set

| Model | Fraud caught (recall) | Flagged that were fraud (precision) | PR-AUC |
|---|---|---|---|
| **Logistic regression (used)** | 95.0% | 98.4% | 0.961 |
| LightGBM (comparison) | 95.8% | 95.3% | 0.898 |
| Logistic regression *without* `empties_balance` | 25.8% | 85.2% | 0.478 |
| PaySim's built-in rule (transfers over 200,000) | 0.7% | 100% | 0.011 |

Fraud is rare (0.5% of test rows), so these tables report recall, precision and PR-AUC rather than accuracy.

## Limitations: read before quoting numbers

1. **The high scores come mostly from one PaySim pattern.** In PaySim, fraudsters almost always send *exactly* the whole balance. In the training data, every such transfer is fraud. Without that feature, recall falls to 26%. Real-world fraud is far less clean, so **these numbers do not predict real-world performance.**
2. **The data doesn't match the domain.** PaySim has no payee whitelist, scam text or bill history. The model is not validated for elderly bill payments in Malaysia.
3. **It doesn't flag the demo scam.** The RM 800 scam transfer is 94% of Ah Por's RM 850 balance, so it scores 0.002. The rules block it anyway. A transfer of the whole RM 850 at 3am scores 0.97.
4. **Fraud rate drifts over time:** 0.10% of training rows versus 0.50% of test rows.

**Safe claim:** *"A fraud model trained on the public PaySim dataset runs in shadow mode alongside the rules. It reached 95% recall on PaySim's held-out data, mostly from the 'empties the account' pattern. It needs real payment data before it can influence decisions."*

## Retrain

```
pip install -r ml/requirements.txt
python ml/train_risk_model.py      # uses your Kaggle token (~/.kaggle/kaggle.json)
```
