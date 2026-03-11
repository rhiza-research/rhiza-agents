# Example Prompts for Weather AI

**Broad meteorological knowledge (TBH not sure how important this is)**

- What are the key drivers of the rainy season in Kenya?
- What phases of MJO are likely to produce rain in Kenya?
- What kinds of clouds produce rain in Northern Ghana?
- How large are the storms in Ghana vs Kenya? Which region usually experiences larger storm systems?
- When there is a tropical cyclone approaching Somalia, how does that impact the rain in Kenya?
- How does the rain vary in West Africa during El Nino and La Nina?

**Historical forecast performance**

- Which forecasting model performs the best at predicting rainfall in Kenya?
    - Ideal answer here would look at key precipitation metrics like MAE and ACC, only compare forecasts at the same Lead and then prompt the user about specific metrics and lead times they would like to dive into. I.e. “Various forecasting models perform better at different lead times. At short lead times forecast like ECMWF IFS and Google’s graphcast have the lowest MAE (xx, xx) and highest ACC (xx, xx) against top precipitation data products like IMERG. At longer, subseasonal lead times ECMWF Extended Range with appropriate debiasing performs the best with an ACC of XX for weekly rain. Here are two table of all forecasts contained in the sheerwater benchmark and there performance in Kenya by lead time. Most meteorologists consider an ACC of > 0.6 to be good for rainfall. Are there specific lead times or metrics you are interested in exploring further?
    - Process → recognize that MAE and ACC are key metrics for precipitation, query sheerwater for tables of historical performance in Kenya for those metrics, summarize the tables with key results, add in meteorological context about what “good” is, add followup question.
- Where in Kenya are forecasts best at predicting the onset of March-April-May Rains?
- In Ghana are forecasts good enough at predicting the onset of the rainy season to warrant dissemination?
- What models should I blend together for predicting rainy season onset in Senegal?
- Compare top forecasts downscaled by several top downscaling methods to the station data provided here <data> and report on which downscaling methods works the best.

**Historical Satellite product performance**

- What satellite products are best at observing the rainy season onset in tropical regions?
- What satellite product has the lowest observation error for rainfall?
- Compare satellite products to the station data provided here <link/upload> on key rainfall metrics.
- Is the ground truth satellite product in Ghana good enough at observing rainy season onset to evaluate rainy season onset forecasts?

**Climatological Knowledge**

**Operational Forecasting**

- Run my WRF model out to the next 10 days and save the output as a netCDF.
- How much has it rained in the last 10 days in Kenya?
- In years like this year, how good have each of the top models been at predicting rainfall in the next three-6 weeks.